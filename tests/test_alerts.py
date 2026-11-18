"""Alerts: event classification, de-duplication, delivery and the worker."""

from __future__ import annotations

import json
import queue
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from timefence import alerts, budget, db
from timefence.api import create_app
from timefence.config import Settings
from timefence.reconciler import Reconciler

from .fakes import StubServer

TZ = ZoneInfo("UTC")
BERLIN = ZoneInfo("Europe/Berlin")
EVENTS = (
    "group_low",
    "group_exhausted",
    "bedtime_started",
    "sync_failing",
    "sync_recovered",
    "weekly_summary",
)


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


def make_group(store, name="YouTube", services=("youtube",), minutes=100, windows=(), domains=()):
    with store as conn:
        return budget.create_group(
            conn,
            name=name,
            emoji="",
            weekday_minutes=minutes,
            weekend_minutes=minutes,
            services=list(services),
            domains=list(domains),
            windows=list(windows),
        )


def spend(store, group, seconds: int, day: str) -> None:
    with store as conn:
        budget.add_metered_seconds(conn, group.id, day, seconds)


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
    assert shown["events"]["group_low"] is True
    assert shown["events"]["group_exhausted"] is True
    assert shown["events"]["sync_failing"] is True
    assert shown["events"]["sync_recovered"] is True
    assert shown["events"]["bedtime_started"] is False
    assert shown["events"]["weekly_summary"] is False
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
    configure(store, events={"weekly_summary": True})
    with store as conn:
        shown = alerts.public_config(conn, ts_utc(2026, 11, 16, 12))
    assert shown["channel"] == "ntfy"
    assert shown["ntfy"]["topic_tail"] == "ne"
    assert shown["ntfy"]["token_configured"] is True
    assert shown["events"]["weekly_summary"] is True

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
        {"events": {"group_low": "maybe"}},
    ):
        with store as conn, pytest.raises(budget.ValidationError):
            alerts.write_config(conn, bad)


# --------------------------------------------------------------------------- #
# event classification and de-duplication
# --------------------------------------------------------------------------- #


def test_low_threshold_edge_and_once_per_day(store, tmp_path):
    settings = settings_for(tmp_path)
    now = ts_utc(2026, 11, 11, 12)  # a Wednesday
    group = make_group(store, minutes=100)
    ntfy_config(store)
    worker = alerts.AlertWorker(store, settings)

    spend(store, group, 5400 - 15, "2026-11-11")  # 615 s left: no message yet
    assert probe(worker, now) == []

    spend(store, group, 15, "2026-11-11")  # exactly 600 s left
    messages = probe(worker, now)
    assert [m.event for m in messages] == ["group_low"]
    assert messages[0].minutes_left == 10
    assert messages[0].key == f"low:{group.id}:2026-11-11"

    assert probe(worker, now) == []  # once per local day
    spend(store, group, 15, "2026-11-11")  # 585 s left
    assert probe(worker, now) == []

    # a restart does not resend it either
    restarted = alerts.AlertWorker(store, settings)
    assert probe(restarted, now) == []


def test_exhausted_fires_on_the_transition_only(store, tmp_path):
    settings = settings_for(tmp_path)
    now = ts_utc(2026, 11, 11, 12)
    group = make_group(store, minutes=100)
    ntfy_config(store)
    worker = alerts.AlertWorker(store, settings)

    spend(store, group, 6000, "2026-11-11")  # budget gone
    messages = probe(worker, now)
    assert [m.event for m in messages] == ["group_exhausted"]
    assert messages[0].minutes_left == 0
    assert probe(worker, now) == []
    assert probe(alerts.AlertWorker(store, settings), now) == []


def test_quiet_states_do_not_alert(store, tmp_path):
    settings = settings_for(tmp_path)
    now = ts_utc(2026, 11, 11, 13, 30)
    paused = make_group(store, name="Paused", services=("youtube",), minutes=100)
    blocked = make_group(store, name="Blocked", services=("roblox",), minutes=100)
    free = make_group(store, name="Free", services=("instagram",), minutes=10)
    ntfy_config(store)
    with store as conn:
        budget.update_group(conn, paused.id, paused=True)
        budget.set_force_block(conn, blocked.id, now + 3600)
        budget.set_free_until(conn, free.id, now + 3600)
        budget.add_metered_seconds(conn, free.id, "2026-11-11", 600, 600)
    assert probe(alerts.AlertWorker(store, settings), now) == []


def test_bedtime_started_is_off_by_default(store, tmp_path):
    settings = settings_for(tmp_path)
    now = ts_utc(2026, 11, 11, 13, 30)
    make_group(store, windows=[{"start": "13:00", "end": "14:00", "days": "all"}])
    ntfy_config(store)
    worker = alerts.AlertWorker(store, settings)
    assert probe(worker, now) == []

    configure(store, events={"bedtime_started": True})
    messages = probe(worker, now)
    assert [m.event for m in messages] == ["bedtime_started"]
    assert messages[0].time_text == "14:00"
    assert probe(worker, now) == []

    # outside the window nothing fires
    configure(store, events={"bedtime_started": True})
    assert probe(alerts.AlertWorker(store, settings), ts_utc(2026, 11, 11, 15)) == []


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


def test_weekly_summary_fires_once_on_monday_morning(store, tmp_path):
    settings = settings_for(tmp_path, tz="Europe/Berlin")
    group = make_group(store, minutes=100)
    configure(store, channel="ntfy", ntfy_topic="topic", events={"weekly_summary": True})
    with store as conn:
        budget.add_metered_seconds(conn, group.id, "2026-10-19", 600)
        budget.add_metered_seconds(conn, group.id, "2026-10-25", 1800)  # the DST day
        budget.add_metered_seconds(conn, group.id, "2026-10-18", 900)  # outside the week
    worker = alerts.AlertWorker(store, settings)

    before = ts_berlin(2026, 10, 26, 7, 59)
    assert probe(worker, before) == []

    monday = ts_berlin(2026, 10, 26, 8, 0)
    messages = probe(worker, monday)
    assert [m.event for m in messages] == ["weekly_summary"]
    assert messages[0].groups == (("YouTube", 40.0),)  # 600 + 1800 s over 7 local days
    assert messages[0].key == "weekly:2026-10-26"
    assert probe(worker, monday + 60) == []


def test_missed_weekly_summary_is_recorded_without_sending(store, tmp_path, caplog):
    settings = settings_for(tmp_path, tz="Europe/Berlin")
    make_group(store, minutes=100)
    configure(store, channel="ntfy", ntfy_topic="topic", events={"weekly_summary": True})
    worker = alerts.AlertWorker(store, settings)

    tuesday = ts_berlin(2026, 10, 27, 9, 0)
    with caplog.at_level("INFO", logger="timefence.alerts"):
        assert probe(worker, tuesday) == []
    assert "weekly:2026-10-26" in sent_keys(store)
    assert "missed weekly summary" in caplog.text


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
    group = make_group(store, minutes=100)
    ntfy_config(store)
    worker = alerts.AlertWorker(store, settings)
    spend(store, group, 5400, "2026-11-11")
    fill_hour(store, "low", alerts.HOURLY_LIMIT, now)

    assert probe(worker, now) == []
    assert f"low:{group.id}:2026-11-11" not in sent_keys(store)
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
    fill_hour(store, "low", alerts.HOURLY_LIMIT, now)

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
        worker.enqueue(alerts.Message(event="group_low", key=f"low:filler-{index}", created_at=0))
    assert worker.queue.qsize() == alerts.QUEUE_SIZE

    worker.enqueue(alerts.Message(event="group_low", key="low:newest", created_at=0))
    queued = [item.key for item in list(worker.queue.queue) if item is not None]
    assert worker.queue.qsize() == alerts.QUEUE_SIZE
    assert "low:filler-0" not in queued
    assert "low:newest" in queued
    assert settings_keys(store)["alerts.dropped"] == "1"

    # nothing of that class left to evict in a fresh queue: the newcomer is dropped
    worker2 = alerts.AlertWorker(store, settings)
    for index in range(alerts.QUEUE_SIZE):
        worker2.enqueue(alerts.Message(event="group_low", key=f"low:other-{index}", created_at=0))
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
            "INSERT INTO alerts_sent(key, sent_at) VALUES('low:1:2026-01-01', ?)",
            (now - 15 * 86400,),
        )
        conn.commit()
    worker = alerts.AlertWorker(store, settings)
    worker.prune(now)
    assert "low:1:2026-01-01" not in sent_keys(store)


# --------------------------------------------------------------------------- #
# delivery
# --------------------------------------------------------------------------- #


def a_message(event="group_low", **overrides) -> alerts.Message:
    values = {
        "event": event,
        "key": f"{event}:1",
        "created_at": ts_utc(2026, 11, 11, 12),
        "time_iso": "2026-11-11T12:00:00+00:00",
        "group": "Игры",
        "minutes_left": 10,
    }
    values.update(overrides)
    return alerts.Message(**values)


def test_ntfy_delivery_puts_names_in_the_utf8_body_only(deliveries):
    with StubServer() as stub:
        target = alerts.Target(url=stub.url + "/kid-alerts", channel="ntfy", host="127.0.0.1")
        assert alerts._deliver(target, a_message()) == "ok"
        request = stub.requests[0]
        assert request["path"] == "/kid-alerts"
        assert request["headers"]["Title"] == "TimeFence group_low"
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
            "group_low": {"event", "group", "minutes_left", "time", "message"},
            "group_exhausted": {"event", "group", "minutes_left", "time", "message"},
            "bedtime_started": {"event", "group", "time", "message"},
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
            if "minutes_left" in keys:
                assert body["minutes_left"] == 10
            if "message" in keys:
                assert body["message"]

        assert (
            alerts._deliver(
                target,
                a_message("weekly_summary", groups=(("YouTube", 40.0), ("Games", 12.5))),
            )
            == "ok"
        )
        weekly = json.loads(stub.requests[-1]["body"])
        assert weekly["event"] == "weekly_summary"
        assert weekly["days"] == 7
        assert weekly["groups"] == [
            {"group": "YouTube", "minutes": 40.0},
            {"group": "Games", "minutes": 12.5},
        ]
        assert "group" not in weekly


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
    make_group(store, minutes=100)
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
    group = make_group(store, minutes=100)
    spend(store, group, 5400, "2026-11-11")
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
    assert [message.event for message in queued if message] == ["group_low"]


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
        worker.enqueue(alerts_module.Message(event="group_low", key="low:1", created_at=0))
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
