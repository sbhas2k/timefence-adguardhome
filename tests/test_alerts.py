"""Alerts: event classification, de-duplication, delivery and the worker."""

from __future__ import annotations

import io
import json
import logging
import queue
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
import requests
from fastapi.testclient import TestClient

from timefence import alerts, budget, db
from timefence.api import create_app
from timefence.config import Settings
from timefence.reconciler import Reconciler

from .fakes import StubServer

TZ = ZoneInfo("UTC")
BERLIN = ZoneInfo("Europe/Berlin")
EVENTS = ("group_closed", "sync_failing", "sync_recovered")


@pytest.fixture()
def store(tmp_path):
    store = db.Store(tmp_path / "alerts.db")
    with store as conn:
        db.migrate(conn, TZ)
    yield store
    store.close()


def ts_utc(year, month, day, hour, minute=0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp())


def ts_berlin(year, month, day, hour, minute=0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=BERLIN).timestamp())


def settings_for(tmp_path, tz="UTC", interval=30) -> Settings:
    return Settings.from_env(
        {
            "ADGUARD_URL": "http://127.0.0.1:3000",
            "ADGUARD_USER": "admin",
            "ADGUARD_PASSWORD": "secret",
            "BASE_PATH": "/",
            "TZ": tz,
            "DATA_DIR": str(tmp_path / "data"),
            "SECURE_COOKIES": "false",
            "RECONCILE_INTERVAL": str(interval),
            "LOG_LEVEL": "WARNING",
        }
    )


def make_group(store, name="YouTube", services=("youtube",), windows=(), domains=()):
    with store as conn:
        return budget.create_group(
            conn,
            name=name,
            emoji="",
            services=list(services),
            domains=list(domains),
            windows=list(windows),
        )


def a_window(start="13:00", end="14:00", days="all") -> dict:
    return {"start": start, "end": end, "days": days}


def configure(store, **values) -> None:
    with store as conn:
        alerts.write_config(conn, values)


def ntfy_config(store, topic="kid-alerts") -> None:
    configure(store, channel="ntfy", ntfy_topic=topic)


def offline() -> tuple[None, None]:
    return None, None


def online(now: int) -> tuple[int, float]:
    return now, time.monotonic()


def probe(worker, now, last_ok=None) -> list:
    """Run one probe and return the messages it queued."""
    last_ok_ts, last_ok_mono = online(now) if last_ok is None else last_ok
    worker.probe(now, last_ok_ts, last_ok_mono)
    out = []
    while True:
        try:
            out.append(worker.queue.get_nowait())
        except queue.Empty:
            break
    return [message for message in out if message is not None]


def settings_keys(store) -> dict:
    with store as conn:
        return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}


def sent_keys(store) -> set[str]:
    with store as conn:
        return {row["key"] for row in conn.execute("SELECT key FROM alerts_sent")}


# --------------------------------------------------------------------------- #
# target parsing
# --------------------------------------------------------------------------- #


def test_parse_target_accepts_lan_and_idn_hosts():
    for raw, host in (
        ("http://127.0.0.1:1/t", "127.0.0.1"),
        ("https://ntfy.example/ntfy", "ntfy.example"),
        ("http://[::1]:8080/t", "::1"),
        ("https://münchen.example/x", "münchen.example"),
        ("https://ntfy.sh/mytopic", "ntfy.sh"),
        ("http://192.168.1.10:8080/t", "192.168.1.10"),
    ):
        target = alerts.parse_target(raw)
        assert target.host == host
        assert target.url.startswith(("http://", "https://"))


def test_parse_target_rejects_what_it_must():
    for raw in (
        "ftp://ntfy.example/topic",
        "file:///etc/passwd",
        "http:///x",
        "http://exa mple.com/",
        "http://u:p@host/",
        "http://host:99999/",
        "http://host/#fragment",
        "http://[fe80::1%25eth0]/t",
        "",
        "not a url",
    ):
        with pytest.raises(budget.ValidationError):
            alerts.parse_target(raw)


# --------------------------------------------------------------------------- #
# config: defaults, masking and merge rules
# --------------------------------------------------------------------------- #


def test_config_defaults_and_masking(store):
    now = ts_utc(2026, 11, 16, 12)
    with store as conn:
        shown = alerts.public_config(conn, now)
    assert shown["channel"] == ""
    assert shown["enabled"] is True
    assert set(shown["events"]) == set(EVENTS)
    assert shown["events"]["group_closed"] is False
    assert shown["events"]["sync_failing"] is True
    assert shown["events"]["sync_recovered"] is True
    assert shown["ntfy"] == {"base_host": "", "topic_tail": "", "token_configured": False}
    assert shown["webhook"] == {"host": "", "path_tail": ""}
    assert shown["sent_last_hour"] == 0

    configure(
        store,
        channel="ntfy",
        ntfy_base="https://ntfy.example/ntfy",
        ntfy_topic="s3cr3t-topic",
        ntfy_token="TOKEN123",
        webhook_url="http://192.168.1.5:8080/private/hook-XY",
    )
    with store as conn:
        shown = alerts.public_config(conn, now)
    text = json.dumps(shown)
    for secret in ("s3cr3t-topic", "TOKEN123", "/private/hook", "hook-XY"):
        assert secret not in text
    assert shown["ntfy"]["topic_tail"] == "ic"  # the last two characters
    assert shown["ntfy"]["token_configured"] is True
    assert shown["ntfy"]["base_host"] == "ntfy.example"
    assert shown["webhook"]["host"] == "192.168.1.5:8080"
    assert shown["webhook"]["path_tail"] == "XY"


def test_config_merge_rules(store):
    configure(store, channel="ntfy", ntfy_topic="topic-one", ntfy_token="tok")
    configure(store, events={"sync_failing": False})
    with store as conn:
        shown = alerts.public_config(conn, ts_utc(2026, 11, 16, 12))
    assert shown["channel"] == "ntfy"
    assert shown["ntfy"]["topic_tail"] == "ne"
    assert shown["ntfy"]["token_configured"] is True
    assert shown["events"]["sync_failing"] is False

    configure(store, ntfy_token="")
    with store as conn:
        assert alerts.public_config(conn, 0)["ntfy"]["token_configured"] is False

    # switching the channel keeps the other channel's stored values
    configure(store, channel="webhook", webhook_url="http://127.0.0.1:9/hook")
    with store as conn:
        shown = alerts.public_config(conn, 0)
    assert shown["channel"] == "webhook"
    assert shown["ntfy"]["topic_tail"] == "ne"


def test_config_rejects_bad_values(store):
    for bad in (
        {"channel": "email"},
        {"channel": "ntfy", "ntfy_topic": "bad topic!"},
        {"channel": "ntfy", "ntfy_topic": "x" * 65},
        {"webhook_url": "ftp://host/hook"},
        {"webhook_url": "http://u:p@host/hook"},
        {"ntfy_base": "file:///etc/passwd"},
        {"events": []},
        {"events": {"group_closed": "maybe"}},
    ):
        with store as conn, pytest.raises(budget.ValidationError):
            alerts.write_config(conn, bad)


# --------------------------------------------------------------------------- #
# event classification and de-duplication
# --------------------------------------------------------------------------- #


def test_group_closed_is_off_by_default(store, tmp_path):
    settings = settings_for(tmp_path)
    now = ts_utc(2026, 11, 11, 12)  # a Wednesday, outside the window
    group = make_group(store, windows=[a_window()])
    ntfy_config(store)
    worker = alerts.AlertWorker(store, settings)
    assert probe(worker, now) == []
    assert sent_keys(store) == set()

    configure(store, events={"group_closed": True})
    messages = probe(worker, now)
    assert [m.event for m in messages] == ["group_closed"]
    assert messages[0].group == "YouTube"
    assert messages[0].time_text == "13:00"  # the next opening, a local HH:MM
    assert sent_keys(store) == {f"closed:{group.id}:{ts_utc(2026, 11, 11, 13)}"}


def test_group_closed_fires_once_per_closing(store, tmp_path):
    settings = settings_for(tmp_path)
    make_group(store, windows=[a_window("13:00", "14:00")])
    ntfy_config(store)
    configure(store, events={"group_closed": True})
    worker = alerts.AlertWorker(store, settings)

    assert [m.event for m in probe(worker, ts_utc(2026, 11, 11, 12))] == ["group_closed"]
    assert probe(worker, ts_utc(2026, 11, 11, 12, 30)) == []  # the same closing
    assert probe(worker, ts_utc(2026, 11, 11, 13, 30)) == []  # inside the window
    # the next day's closing is a new event, and a restart does not resend it
    restarted = alerts.AlertWorker(store, settings)
    assert [m.event for m in probe(restarted, ts_utc(2026, 11, 12, 12))] == ["group_closed"]
    assert probe(restarted, ts_utc(2026, 11, 12, 12, 45)) == []
    assert len([key for key in sent_keys(store) if key.startswith("closed:")]) == 2


def test_a_wrapping_window_does_not_alert_again_after_midnight(store, tmp_path):
    settings = settings_for(tmp_path)
    make_group(store, windows=[a_window("21:00", "07:00")])
    ntfy_config(store)
    configure(store, events={"group_closed": True})
    worker = alerts.AlertWorker(store, settings)

    assert probe(worker, ts_utc(2026, 11, 9, 23)) == []  # inside the window
    first = probe(worker, ts_utc(2026, 11, 10, 7, 30))  # the window closed at 07:00
    assert [m.event for m in first] == ["group_closed"]
    assert first[0].time_text == "21:00"
    assert probe(worker, ts_utc(2026, 11, 10, 8)) == []
    assert probe(worker, ts_utc(2026, 11, 10, 15)) == []
    # it opens at 21:00 and closes again the next morning
    assert probe(worker, ts_utc(2026, 11, 10, 22)) == []
    second = probe(worker, ts_utc(2026, 11, 11, 8))
    assert [m.event for m in second] == ["group_closed"]


def test_quiet_states_do_not_alert(store, tmp_path):
    settings = settings_for(tmp_path)
    now = ts_utc(2026, 11, 11, 12)
    paused = make_group(store, name="Paused", services=("youtube",), windows=[a_window()])
    blocked = make_group(store, name="Blocked", services=("roblox",), windows=[a_window()])
    free = make_group(store, name="Free", services=("instagram",), windows=[a_window()])
    make_group(store, name="Always", services=("discord",))
    ntfy_config(store)
    configure(store, events={"group_closed": True})
    with store as conn:
        budget.update_group(conn, paused.id, paused=True)
        budget.set_force_block(conn, blocked.id, now + 3600)
        budget.set_free_until(conn, free.id, now + 3600)
    assert probe(alerts.AlertWorker(store, settings), now) == []


def test_free_time_suppresses_group_closed(store, tmp_path):
    settings = settings_for(tmp_path)
    now = ts_utc(2026, 11, 11, 12)
    group = make_group(store, windows=[a_window("08:00", "09:00")])
    ntfy_config(store)
    configure(store, events={"group_closed": True})
    with store as conn:
        budget.set_free_until(conn, group.id, now + 3600)
    worker = alerts.AlertWorker(store, settings)
    assert probe(worker, now) == []

    # once the free window ends the schedule closes the group again
    messages = probe(worker, now + 3601)
    assert [m.event for m in messages] == ["group_closed"]


def test_sync_failing_needs_three_runs_and_is_once_per_outage(store, tmp_path):
    settings = settings_for(tmp_path, interval=30)
    now = ts_utc(2026, 11, 11, 12)
    ntfy_config(store)
    worker = alerts.AlertWorker(store, settings)

    assert probe(worker, now, offline()) == []
    assert probe(worker, now, offline()) == []
    messages = probe(worker, now, offline())
    assert [m.event for m in messages] == ["sync_failing"]
    assert messages[0].key == f"sync_failing:{now}"
    assert probe(worker, now, offline()) == []

    # a restart during the same outage does not repeat it
    restarted = alerts.AlertWorker(store, settings)
    for _ in range(3):
        assert probe(restarted, now + 60, offline()) == []
    assert settings_keys(store)["alerts.outage_started_at"] == str(now)

    # the first fresh run after the outage reports exactly one recovery
    recovered = probe(restarted, now + 120)
    assert [m.event for m in recovered] == ["sync_recovered"]
    assert recovered[0].outage_seconds == 120
    assert probe(restarted, now + 150) == []
    assert settings_keys(store).get("alerts.outage_started_at", "") == ""


def test_sync_failing_does_not_flap_or_rearm_on_a_clock_jump(store, tmp_path):
    settings = settings_for(tmp_path, interval=30)
    ntfy_config(store)
    worker = alerts.AlertWorker(store, settings)
    now = ts_utc(2026, 11, 11, 12)

    # failing, failing, success, failing, failing: never three in a row
    assert probe(worker, now, offline()) == []
    assert probe(worker, now, offline()) == []
    assert probe(worker, now) == []
    assert probe(worker, now, offline()) == []
    assert probe(worker, now, offline()) == []
    assert probe(worker, now) == []

    # arm once, then a wall-clock jump must not send a second message
    for _ in range(3):
        probe(worker, now, offline())
    assert probe(worker, now + 6 * 3600, offline()) == []
    assert len([key for key in sent_keys(store) if key.startswith("sync_failing:")]) == 1


# --------------------------------------------------------------------------- #
# rate limit, protected classes and the queue
# --------------------------------------------------------------------------- #


def fill_hour(store, prefix: str, count: int, now: int) -> None:
    with store as conn:
        for index in range(count):
            conn.execute(
                "INSERT INTO alerts_sent(key, sent_at) VALUES(?,?)",
                (f"{prefix}:filler-{index}", now - 60),
            )
        conn.commit()


def test_hourly_limit_drops_routine_messages(store, tmp_path):
    settings = settings_for(tmp_path)
    now = ts_utc(2026, 11, 11, 12)
    group = make_group(store, windows=[a_window()])
    ntfy_config(store)
    configure(store, events={"group_closed": True})
    worker = alerts.AlertWorker(store, settings)
    fill_hour(store, "closed", alerts.HOURLY_LIMIT, now)

    assert probe(worker, now) == []
    assert f"closed:{group.id}:{ts_utc(2026, 11, 11, 13)}" not in sent_keys(store)
    assert settings_keys(store)["alerts.dropped"] == "1"
    with store as conn:
        shown = alerts.public_config(conn, now)
    assert shown["dropped_last_hour"] == 1
    assert shown["sent_last_hour"] == alerts.HOURLY_LIMIT


def test_protected_classes_survive_routine_chatter(store, tmp_path):
    settings = settings_for(tmp_path, interval=30)
    now = ts_utc(2026, 11, 11, 12)
    ntfy_config(store)
    worker = alerts.AlertWorker(store, settings)
    fill_hour(store, "closed", alerts.HOURLY_LIMIT, now)

    messages = []
    for _ in range(3):
        messages += probe(worker, now, offline())
    assert [m.event for m in messages] == ["sync_failing"]
    assert settings_keys(store).get("alerts.dropped", "0") == "0"


def test_a_protected_class_is_dropped_at_its_own_limit(store, tmp_path):
    settings = settings_for(tmp_path, interval=30)
    now = ts_utc(2026, 11, 11, 12)
    ntfy_config(store)
    worker = alerts.AlertWorker(store, settings)
    fill_hour(store, "sync_failing", alerts.HOURLY_LIMIT, now)

    messages = []
    for _ in range(3):
        messages += probe(worker, now, offline())
    assert messages == []
    assert settings_keys(store)["alerts.dropped"] == "1"


def test_a_full_queue_drops_the_oldest_of_the_incoming_class(store, tmp_path):
    settings = settings_for(tmp_path)
    ntfy_config(store)
    worker = alerts.AlertWorker(store, settings)
    for index in range(alerts.QUEUE_SIZE):
        worker.enqueue(
            alerts.Message(event="group_closed", key=f"closed:filler-{index}", created_at=0)
        )
    assert worker.queue.qsize() == alerts.QUEUE_SIZE

    worker.enqueue(alerts.Message(event="group_closed", key="closed:newest", created_at=0))
    queued = [item.key for item in list(worker.queue.queue) if item is not None]
    assert worker.queue.qsize() == alerts.QUEUE_SIZE
    assert "closed:filler-0" not in queued
    assert "closed:newest" in queued
    assert settings_keys(store)["alerts.dropped"] == "1"

    # nothing of that class left to evict in a fresh queue: the newcomer is dropped
    worker2 = alerts.AlertWorker(store, settings)
    for index in range(alerts.QUEUE_SIZE):
        worker2.enqueue(
            alerts.Message(event="group_closed", key=f"closed:other-{index}", created_at=0)
        )
    worker2.enqueue(alerts.Message(event="sync_failing", key="sync_failing:1", created_at=0))
    assert worker2.queue.qsize() == alerts.QUEUE_SIZE
    assert "sync_failing:1" not in [
        item.key for item in list(worker2.queue.queue) if item is not None
    ]
    assert settings_keys(store)["alerts.dropped"] == "2"


def test_old_dedupe_rows_are_pruned(store, tmp_path):
    settings = settings_for(tmp_path)
    now = int(time.time())
    with store as conn:
        conn.execute(
            "INSERT INTO alerts_sent(key, sent_at) VALUES('closed:1:1800000000', ?)",
            (now - 15 * 86400,),
        )
        conn.commit()
    worker = alerts.AlertWorker(store, settings)
    worker.prune(now)
    assert "closed:1:1800000000" not in sent_keys(store)


# --------------------------------------------------------------------------- #
# delivery
# --------------------------------------------------------------------------- #


def a_message(event="group_closed", **overrides) -> alerts.Message:
    values = {
        "event": event,
        "key": f"{event}:1",
        "created_at": ts_utc(2026, 11, 11, 12),
        "time_iso": "2026-11-11T12:00:00+00:00",
        "group": "Игры",
    }
    values.update(overrides)
    return alerts.Message(**values)


def test_ntfy_delivery_puts_names_in_the_utf8_body_only(deliveries):
    with StubServer() as stub:
        target = alerts.Target(url=stub.url + "/kid-alerts", channel="ntfy", host="127.0.0.1")
        assert alerts._deliver(target, a_message()) == "ok"
        request = stub.requests[0]
        assert request["path"] == "/kid-alerts"
        assert request["headers"]["Title"] == "TimeFence group_closed"
        assert isinstance(request["body"], bytes)
        assert "Игры" in request["body"].decode("utf-8")
        for name, value in request["headers"].items():
            assert all(ord(character) < 128 for character in value), name

        # a newline in a name is a body problem, never a header problem
        assert alerts._deliver(target, a_message(group="a\nb")) == "ok"
        assert "a\nb" in stub.requests[1]["body"].decode("utf-8")


def test_ntfy_token_is_sent_as_a_header(deliveries):
    with StubServer() as stub:
        target = alerts.Target(
            url=stub.url + "/t", channel="ntfy", host="127.0.0.1", token="secret-token"
        )
        assert alerts._deliver(target, a_message()) == "ok"
        assert stub.requests[0]["headers"]["Authorization"] == "Bearer secret-token"


def test_webhook_bodies_have_the_documented_shape(deliveries):
    with StubServer() as stub:
        target = alerts.parse_target(stub.url + "/hook")
        cases = {
            "group_closed": {"event", "group", "time", "message"},
            "sync_failing": {"event", "time", "message", "outage_seconds"},
            "sync_recovered": {"event", "time", "message", "outage_seconds"},
        }
        for event, keys in cases.items():
            message = a_message(
                event,
                key=f"{event}:1",
                time_text="07:00",
                outage_seconds=42,
            )
            assert alerts._deliver(target, message) == "ok"
            body = json.loads(stub.requests[-1]["body"])
            assert set(body) == keys, event
            assert body["event"] == event
            assert body["time"] == "2026-11-11T12:00:00+00:00"
            if "group" in keys:
                assert body["group"] == "Игры"
            if "message" in keys:
                assert body["message"]

        for request in stub.requests:
            body = json.loads(request["body"])
            for gone in ("minutes_left", "groups", "days"):
                assert gone not in body, gone


def test_no_redirect_is_followed(deliveries):
    with StubServer(status=302) as stub:
        stub.location = stub.url + "/somewhere-else"
        target = alerts.parse_target(stub.url + "/hook")
        assert alerts._deliver(target, a_message()) == "http_3xx"
        assert len(stub.requests) == 1
        assert stub.requests[0]["path"] == "/hook"


def test_http_and_transport_failures_become_categories(deliveries):
    with StubServer(status=500) as stub:
        assert alerts._deliver(alerts.parse_target(stub.url + "/h"), a_message()) == "http_500"
    refused = alerts.parse_target("http://127.0.0.1:1/hook")
    assert alerts._deliver(refused, a_message()) == "connect"


def test_delivery_never_raises_when_the_transport_does(store, tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    ntfy_config(store)
    worker = alerts.AlertWorker(store, settings)

    def boom(target, message):
        raise RuntimeError("broken transport")

    monkeypatch.setattr(alerts, "_deliver", boom)
    assert worker._deliver_message(a_message()) is True
    assert alerts.send_test(store, settings) == "connect"


def test_delivery_failures_are_logged_once_without_secrets(
    store, tmp_path, monkeypatch, caplog, deliveries
):
    settings = settings_for(tmp_path)
    configure(store, channel="webhook", webhook_url="http://127.0.0.1:1/private/hook-TOKEN123")
    worker = alerts.AlertWorker(store, settings)
    message = a_message()

    with caplog.at_level("WARNING", logger="timefence.alerts"):
        assert worker._deliver_message(message) is True
        assert worker._deliver_message(message) is True
    assert caplog.text.count("alert delivery failed: webhook (connect)") == 1
    assert "TOKEN123" not in caplog.text
    assert "/private/hook" not in caplog.text
    assert "127.0.0.1:1" not in caplog.text
    assert "Traceback" not in caplog.text

    with StubServer() as stub:
        configure(store, channel="webhook", webhook_url=stub.url + "/ok")
        with caplog.at_level("INFO", logger="timefence.alerts"):
            assert worker._deliver_message(message) is True
        assert "alert delivery recovered (webhook)" in caplog.text


def test_a_cleared_channel_drops_the_message(store, tmp_path, deliveries):
    settings = settings_for(tmp_path)
    ntfy_config(store)
    worker = alerts.AlertWorker(store, settings)
    configure(store, channel="")
    with StubServer() as stub:
        configure(store, channel="webhook", webhook_url=stub.url + "/hook")
        configure(store, channel="")
        assert worker._deliver_message(a_message()) is True
        assert stub.requests == []


# --------------------------------------------------------------------------- #
# the probe and the reconciler
# --------------------------------------------------------------------------- #


def test_probe_is_cheap_and_never_touches_run_state(store, fake, tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    ntfy_config(store)
    make_group(store)
    reconciler = Reconciler(store, fake, TZ)
    reconciler.alert_worker = alerts.AlertWorker(store, settings)

    def broken(*args, **kwargs):
        raise ValueError("broken facts")

    monkeypatch.setattr(reconciler.alert_worker, "_probe", broken)
    result = reconciler.run("test", now=ts_utc(2026, 11, 11, 12))

    assert result["errors"] == []
    assert reconciler.last_error is None
    assert reconciler.last_ok_ts is not None  # the run still synced
    with store as conn:
        rows = conn.execute("SELECT COUNT(*) FROM events WHERE kind LIKE 'alert%'").fetchone()
        assert rows[0] == 0


def test_probe_runs_through_the_reconciler(store, fake, tmp_path):
    settings = settings_for(tmp_path)
    ntfy_config(store)
    configure(store, events={"group_closed": True})
    make_group(store, windows=[a_window()])
    reconciler = Reconciler(store, fake, TZ)
    worker = alerts.AlertWorker(store, settings)
    reconciler.alert_worker = worker

    reconciler.run("test", now=ts_utc(2026, 11, 11, 12))

    queued = []
    while True:
        try:
            queued.append(worker.queue.get_nowait())
        except queue.Empty:
            break
    assert [message.event for message in queued if message] == ["group_closed"]


def test_worker_starts_with_the_app_and_stops_before_the_store(settings, fake, monkeypatch, caplog):
    from timefence import alerts as alerts_module

    app = create_app(settings, adguard=fake)
    seen = {}

    def slow_delivery(target, message):
        seen["started"] = True
        time.sleep(4.0)
        return "ok"

    with TestClient(app):
        worker = app.state.alerts
        assert isinstance(worker, alerts_module.AlertWorker)
        assert worker.is_alive()
        with app.state.store as conn:
            alerts_module.write_config(
                conn, {"channel": "webhook", "webhook_url": "http://127.0.0.1:9/hook"}
            )
        monkeypatch.setattr(alerts_module, "_deliver", slow_delivery)
        worker.enqueue(alerts_module.Message(event="group_closed", key="closed:1", created_at=0))
        deadline = time.monotonic() + 3
        while not seen.get("started") and time.monotonic() < deadline:
            time.sleep(0.01)
        assert seen.get("started") is True

    # a pending message may be dropped at shutdown on purpose; this one was
    # already in flight, so shutdown has to wait, warn once and let it finish
    assert seen.get("started") is True
    worker.join(6)
    assert not worker.is_alive()
    assert "did not stop" in caplog.text
    assert "the database is closed" in caplog.text
    assert "Traceback" not in caplog.text
    assert "ProgrammingError" not in caplog.text


def test_without_the_app_lifespan_no_worker_is_started(settings, fake):
    app = create_app(settings, adguard=fake)
    assert getattr(app.state, "alerts", None) is None
    client = TestClient(app)  # no context: the lifespan never runs
    try:
        assert app.state.alerts is None
    finally:
        client.close()


def test_alert_config_never_writes_an_event_row(store):
    ntfy_config(store, topic="TOKEN123")
    with store as conn:
        events = conn.execute("SELECT kind, detail FROM events").fetchall()
        settings_rows = {
            row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")
        }
    assert [row["kind"] for row in events if row["kind"].startswith("alert")] == []
    assert all("TOKEN123" not in (row["detail"] or "") for row in events)
    assert settings_rows["alerts.ntfy_topic"] == "TOKEN123"


class _FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status_code = status
        self.raw = io.BytesIO(b"ignored")
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_transport_is_configured_safely(monkeypatch, deliveries):
    assert alerts.SESSION.trust_env is False
    calls: list[tuple[str, dict]] = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeResponse()

    monkeypatch.setattr(alerts.SESSION, "post", fake_post)
    target = alerts.Target(url="http://127.0.0.1:9/hook", channel="webhook", host="127.0.0.1")
    assert alerts._deliver(target, a_message()) == "ok"

    url, kwargs = calls[0]
    assert url == "http://127.0.0.1:9/hook"
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == (3, 2)
    assert kwargs["stream"] is True


def test_shutdown_waits_for_a_fast_delivery(settings, fake, monkeypatch, caplog):
    app = create_app(settings, adguard=fake)
    seen: dict = {}

    def fast_delivery(target, message):
        seen["started"] = True
        time.sleep(1.0)
        return "ok"

    with TestClient(app):
        worker = app.state.alerts
        with app.state.store as conn:
            alerts.write_config(
                conn, {"channel": "webhook", "webhook_url": "http://127.0.0.1:9/hook"}
            )
        monkeypatch.setattr(alerts, "_deliver", fast_delivery)
        worker.enqueue(alerts.Message(event="group_closed", key="closed:1", created_at=0))
        deadline = time.monotonic() + 3
        while not seen.get("started") and time.monotonic() < deadline:
            time.sleep(0.01)
        assert seen.get("started") is True

    # the delivery finished inside the join window, so shutdown was clean
    assert not worker.is_alive()
    assert "did not stop" not in caplog.text
    assert "the database is closed" not in caplog.text
    conn = db.connect(settings.db_path)
    try:
        assert db.get_setting(conn, "alerts.last_result") == "ok"
    finally:
        conn.close()


def test_a_trickling_delivery_ends_as_a_timeout(deliveries):
    message = alerts.Message(
        event="sync_failing",
        key="sync_failing:1",
        created_at=ts_utc(2026, 11, 11, 12),
        time_iso="2026-11-11T12:00:00Z",
    )
    with StubServer(body=b"x" * 40, trickle=0.5) as stub:
        target = alerts.parse_target(stub.url + "/hook")
        started = time.monotonic()
        category = deliveries(target, message)
        elapsed = time.monotonic() - started
    assert category == "timeout"
    assert elapsed < 7.5, elapsed


# --------------------------------------------------------------------------- #
# telegram
# --------------------------------------------------------------------------- #

TOKEN = "8471903:AAH2fake-Token_for_TESTS_0123456789ab"
CHAT = "-1002233445566"


def telegram_config(store, **overrides) -> None:
    values = {"channel": "telegram", "telegram_token": TOKEN, "telegram_chat": CHAT}
    values.update(overrides)
    configure(store, **values)


def telegram_target(store) -> alerts.Target:
    """The fixed-host target the channel builds; no user URL is involved."""
    with store as conn:
        target = alerts._target_for(alerts.read_config(conn))
    assert target is not None
    assert target.channel == "telegram"
    assert target.host == "api.telegram.org"
    assert target.port == 443
    assert target.url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    return target


def capture_posts(monkeypatch, status: int = 200) -> list:
    """Record the requests the sender would make; the real network is never used."""
    calls: list[tuple[str, dict]] = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeResponse(status)

    monkeypatch.setattr(alerts.SESSION, "post", fake_post)
    return calls


def test_telegram_config_is_masked_and_merged(store):
    now = ts_utc(2026, 11, 16, 12)
    with store as conn:
        shown = alerts.public_config(conn, now)
    assert shown["telegram"] == {"token_tail": "", "chat_tail": "", "configured": False}

    telegram_config(store)
    with store as conn:
        shown = alerts.public_config(conn, now)
    text = json.dumps(shown)
    assert TOKEN not in text
    assert CHAT not in text
    assert shown["channel"] == "telegram"
    assert shown["telegram"] == {"token_tail": "ab", "chat_tail": "66", "configured": True}

    # an absent key keeps the stored value, "" clears it
    configure(store, enabled=False)
    with store as conn:
        shown = alerts.public_config(conn, now)
    assert shown["telegram"]["configured"] is True
    assert shown["enabled"] is False

    configure(store, telegram_chat="")
    with store as conn:
        shown = alerts.public_config(conn, now)
    assert shown["telegram"]["chat_tail"] == ""
    assert shown["telegram"]["configured"] is False
    assert shown["telegram"]["token_tail"] == "ab"


def test_telegram_config_validation(store):
    for good in (
        {"telegram_token": "12345:" + "a" * 30, "telegram_chat": "1"},
        {"telegram_token": "1234567890123456:" + "A-z_0" * 10, "telegram_chat": "-1002233445566"},
        {"telegram_token": TOKEN, "telegram_chat": "12345678901234567890"},
    ):
        with store as conn:
            alerts.write_config(conn, dict(channel="telegram", **good))

    for bad in (
        {"telegram_token": "12345:short"},
        {"telegram_token": "12345:" + "a" * 51},
        {"telegram_token": "1234:" + "a" * 30},
        {"telegram_token": "8471903:AAH2 fake token 0123456789abcdefgh"},
        {"telegram_token": "8471903:AAH2fake-Token_for_TESTS_0123456789a!"},
        {"telegram_chat": "@mychat"},
        {"telegram_chat": "12 34"},
        {"telegram_chat": "123456789012345678901"},
        {"telegram_chat": "-"},
        {"telegram_chat": "abc"},
        {"telegram_chat": "1.5"},
    ):
        with store as conn, pytest.raises(budget.ValidationError):
            alerts.write_config(conn, dict(channel="telegram", **bad))


def test_telegram_request_shape(deliveries, store, monkeypatch):
    telegram_config(store)
    target = telegram_target(store)
    calls = capture_posts(monkeypatch)

    assert alerts._deliver(target, a_message()) == "ok"
    assert alerts._deliver(target, a_message("sync_failing", key="sync_failing:1")) == "ok"

    url, kwargs = calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == (3, 2)
    assert kwargs["headers"] == {"Content-Type": "application/json"}
    body = json.loads(kwargs["data"])
    assert body == {
        "chat_id": CHAT,
        "text": alerts.message_text(a_message()),
        "disable_notification": True,
    }
    assert "Игры" in body["text"]
    assert TOKEN not in json.dumps(body)  # the token only travels in the URL path
    assert json.loads(calls[1][1]["data"])["disable_notification"] is False


def test_telegram_delivery_categories(deliveries, store, monkeypatch):
    telegram_config(store)
    target = telegram_target(store)
    for status, expected in (
        (200, "ok"),
        (400, "http_400"),
        (401, "http_401"),
        (403, "http_403"),
        (404, "http_404"),
        (302, "http_3xx"),
        (500, "http_500"),
    ):
        capture_posts(monkeypatch, status)
        assert alerts._deliver(target, a_message()) == expected, status


def test_telegram_transport_failures_are_categories(deliveries, store, monkeypatch):
    telegram_config(store)
    target = telegram_target(store)

    def timeout(url, **kwargs):
        raise requests.exceptions.Timeout()

    def refused(url, **kwargs):
        raise requests.exceptions.ConnectionError()

    monkeypatch.setattr(alerts.SESSION, "post", timeout)
    assert alerts._deliver(target, a_message()) == "timeout"
    monkeypatch.setattr(alerts.SESSION, "post", refused)
    assert alerts._deliver(target, a_message()) == "connect"


def test_telegram_secrets_never_reach_any_log(store, tmp_path, monkeypatch, caplog, deliveries):
    settings = settings_for(tmp_path)
    telegram_config(store)
    seen: list[str] = []

    def fake_post(url, **kwargs):
        # what the transport would print at DEBUG if nothing silenced it
        logging.getLogger("urllib3.connectionpool").debug(
            "Starting new HTTPS connection: POST %s", url
        )
        seen.append(url)
        return _FakeResponse()

    monkeypatch.setattr(alerts.SESSION, "post", fake_post)
    with caplog.at_level("DEBUG"):
        assert alerts.send_test(store, settings) == "ok"

    assert seen and TOKEN in seen[0]  # the URL really carries the token
    assert TOKEN not in caplog.text
    assert CHAT not in caplog.text
    assert "sendMessage" not in caplog.text
    assert logging.getLogger("urllib3").getEffectiveLevel() == logging.WARNING


def test_telegram_needs_a_token_and_a_chat(store, tmp_path, monkeypatch, deliveries):
    settings = settings_for(tmp_path)
    configure(store, channel="telegram", telegram_token=TOKEN)
    assert alerts.send_test(store, settings) == "config"
    with store as conn:
        assert alerts._target_for(alerts.read_config(conn)) is None

    configure(store, telegram_chat=CHAT)
    calls = capture_posts(monkeypatch)
    assert alerts.send_test(store, settings) == "ok"
    assert len(calls) == 1
    assert calls[0][1]["headers"] == {"Content-Type": "application/json"}
    with store as conn:
        assert db.get_setting(conn, "alerts.last_result") == "ok"


def test_a_cleared_telegram_channel_drops_the_message(store, tmp_path):
    settings = settings_for(tmp_path)
    telegram_config(store)
    worker = alerts.AlertWorker(store, settings)
    telegram_config(store)
    configure(store, channel="")

    # the guard fixture would raise here if the worker tried to send anything
    assert worker._deliver_message(a_message()) is True
    with store as conn:
        assert db.get_setting(conn, "alerts.last_result", "") == ""


def test_telegram_ignores_a_hand_edited_token(store, tmp_path):
    """Only write_config stores these values; a tampered row must not build a URL."""
    settings = settings_for(tmp_path)
    with store as conn:
        for key, value in (
            ("alerts.channel", "telegram"),
            ("alerts.telegram_token", "12345:ok?x=https://evil.example"),
            ("alerts.telegram_chat", CHAT),
        ):
            db.set_setting(conn, key, value)
    with store as conn:
        assert alerts._target_for(alerts.read_config(conn)) is None
        shown = alerts.public_config(conn, 0)
    assert shown["telegram"]["configured"] is True  # both fields hold text
    assert alerts.send_test(store, settings) == "config"
