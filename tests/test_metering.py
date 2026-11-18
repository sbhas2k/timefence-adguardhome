"""Metering: time parsing, activity buckets, query-log health and limits."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from timefence import budget, db
from timefence.api import create_app
from timefence.discover import parse_time
from timefence.reconciler import BUCKET, Reconciler

TZ = ZoneInfo("UTC")


@pytest.fixture()
def store(tmp_path):
    store = db.Store(tmp_path / "m.db")
    with store as conn:
        db.migrate(conn, TZ)
    yield store
    store.close()


def add_group(conn, name="YouTube", services=("youtube",), domains=(), minutes=60):
    return budget.create_group(
        conn,
        name=name,
        emoji="",
        weekday_minutes=minutes,
        weekend_minutes=minutes,
        services=list(services),
        domains=list(domains),
    )


def stamp(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S")


def entry(ts: int, host: str = "www.youtube.com", reason: str = "NotFilteredNotFound") -> dict:
    return {
        "time": f"{stamp(ts)}.889389948Z",
        "client": "192.168.1.20",
        "reason": reason,
        "question": {"name": host, "type": "A"},
    }


def settle(store, fake, now: int, entries: list[dict], reason: str = "loop") -> int:
    fake.entries = entries
    return Reconciler(store, fake, TZ).meter(now, reason)


def ts_utc(year, month, day, hour, minute, second=0) -> int:
    return int(datetime(year, month, day, hour, minute, second, tzinfo=UTC).timestamp())


# --------------------------------------------------------------------------- #
# timestamps
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-11-12T16:37:48.889389948Z", ts_utc(2026, 11, 12, 16, 37, 48)),
        ("2026-11-12T16:37:48Z", ts_utc(2026, 11, 12, 16, 37, 48)),
        ("2026-11-12T22:07:48.889389948+05:30", ts_utc(2026, 11, 12, 16, 37, 48)),
        ("2026-11-12T11:37:48.889389948-05:00", ts_utc(2026, 11, 12, 16, 37, 48)),
        ("2026-11-12T11:37:48-0500", ts_utc(2026, 11, 12, 16, 37, 48)),
        ("2026-11-12 16:37:48", ts_utc(2026, 11, 12, 16, 37, 48)),
        ("2026-11-12T11:37:48.889389948-05:00", ts_utc(2026, 11, 12, 16, 37, 48)),
        ("", None),
        ("garbage", None),
        ("2026-11-12T16:37:48+", None),
    ],
)
def test_parse_time_offsets(raw, expected):
    assert parse_time(raw) == expected


# --------------------------------------------------------------------------- #
# activity buckets
# --------------------------------------------------------------------------- #


def test_one_query_charges_one_bucket(store, fake):
    now = ts_utc(2026, 11, 12, 16, 0, 30)
    with store as conn:
        group = add_group(conn)
        db.set_setting(conn, "meter_last_ts", str(now - 30))

    charged = settle(store, fake, now, [entry(now - 5)])

    assert charged == 1
    with store as conn:
        assert budget.metered_seconds(conn, group.id, budget.day_str(now, TZ)) == BUCKET


def test_same_bucket_across_two_ticks_charged_once(store, fake):
    now = ts_utc(2026, 11, 12, 16, 0, 30)
    with store as conn:
        group = add_group(conn)
        db.set_setting(conn, "meter_last_ts", str(now - 20))

    assert settle(store, fake, now, [entry(now - 5)]) == 1
    with store as conn:
        db.set_setting(conn, "meter_last_ts", str(now - 20))  # as if a tick lost the cursor
    assert settle(store, fake, now, [entry(now - 5)]) == 0

    with store as conn:
        assert budget.metered_seconds(conn, group.id, budget.day_str(now, TZ)) == BUCKET


def test_short_window_keeps_the_cursor(store, fake):
    base = ts_utc(2026, 11, 12, 16, 0, 0)
    with store as conn:
        group = add_group(conn)
        db.set_setting(conn, "meter_last_ts", str(base))

    # an admin action two seconds later must not drop the window
    assert settle(store, fake, base + 3, []) == 0
    with store as conn:
        assert int(db.get_setting(conn, "meter_last_ts") or 0) == base + 3

    # a hit 20 s after the first tick is still charged
    assert settle(store, fake, base + 30, [entry(base + 20)]) == 1
    with store as conn:
        assert budget.metered_seconds(conn, group.id, budget.day_str(base, TZ)) == BUCKET


def test_gap_up_to_30_minutes_is_charged(store, fake):
    base = ts_utc(2026, 11, 12, 16, 0, 0)
    with store as conn:
        group = add_group(conn)
        db.set_setting(conn, "meter_last_ts", str(base))

    assert settle(store, fake, base + 600, [entry(base + 300)]) == 1
    with store as conn:
        assert budget.metered_seconds(conn, group.id, budget.day_str(base, TZ)) == BUCKET


def test_pagination_reads_all_pages(store, fake):
    start = 1_700_000_010  # a multiple of BUCKET
    now = start + 1099
    with store as conn:
        group = add_group(conn)
        db.set_setting(conn, "meter_last_ts", str(start))

    entries = [entry(start + offset) for offset in range(1100)]
    settle(store, fake, now, entries)

    pages = [call for call in fake.calls if call[0] == "querylog_page"]
    assert len(pages) == 3
    # entries start+1 .. start+1099 cover 74 buckets of 15 s
    with store as conn:
        assert budget.metered_seconds(conn, group.id, budget.day_str(now, TZ)) == 74 * BUCKET


def test_midnight_window_splits_across_days(store, fake):
    base = ts_utc(2026, 11, 12, 23, 59, 40)
    now = base + 40  # 00:00:20 on the next local day
    with store as conn:
        group = add_group(conn)
        db.set_setting(conn, "meter_last_ts", str(base))

    settle(store, fake, now, [entry(base + 10), entry(base + 30)])

    first_day = budget.day_str(base + 10, TZ)
    second_day = budget.day_str(base + 30, TZ)
    assert first_day != second_day
    with store as conn:
        assert budget.metered_seconds(conn, group.id, first_day) == BUCKET
        assert budget.metered_seconds(conn, group.id, second_day) == BUCKET


# --------------------------------------------------------------------------- #
# what counts as usage
# --------------------------------------------------------------------------- #


def test_blocked_answers_are_not_usage(store, fake):
    now = ts_utc(2026, 11, 12, 16, 0, 30)
    with store as conn:
        group = add_group(conn)
        db.set_setting(conn, "meter_last_ts", str(now - 30))

    settle(
        store,
        fake,
        now,
        [
            entry(now - 20, reason="FilteredBlockedService"),
            entry(now - 15, reason="FilteredBlackList"),
            entry(now - 10, reason="FilteredInvalid"),
        ],
    )

    with store as conn:
        assert budget.metered_seconds(conn, group.id, budget.day_str(now, TZ)) == 0


def test_safe_search_rewrites_are_usage(store, fake):
    now = ts_utc(2026, 11, 12, 16, 0, 30)
    with store as conn:
        group = add_group(conn)
        db.set_setting(conn, "meter_last_ts", str(now - 30))

    settle(
        store,
        fake,
        now,
        [
            entry(now - 20, reason="FilteredSafeSearch"),
            entry(now - 5, reason="Rewrite"),
        ],
    )

    with store as conn:
        assert budget.metered_seconds(conn, group.id, budget.day_str(now, TZ)) == BUCKET


def test_free_allow_is_not_metered(store, fake):
    now = ts_utc(2026, 11, 12, 16, 0, 30)
    with store as conn:
        group = add_group(conn)
        db.set_setting(conn, "meter_last_ts", str(now - 30))
        budget.set_free_until(conn, group.id, now + 3600)

    assert settle(store, fake, now, [entry(now - 5)]) == 0
    with store as conn:
        assert budget.metered_seconds(conn, group.id, budget.day_str(now, TZ)) == 0


def test_restart_catches_up_within_limit(store, fake):
    now = 1_700_000_000
    with store as conn:
        group = add_group(conn)
        db.set_setting(conn, "meter_last_ts", str(now - 4000))

    # a fresh reconciler has no in-memory bucket memory, like after a restart
    charged = settle(store, fake, now, [entry(now - 100), entry(now - 2000)])

    assert charged == 1
    with store as conn:
        assert budget.metered_seconds(conn, group.id, budget.day_str(now, TZ)) == BUCKET


# --------------------------------------------------------------------------- #
# query-log health (admin state only)
# --------------------------------------------------------------------------- #


def login_admin(client) -> None:
    login = client.post("/api/login", json={"username": "admin", "password": "admin"})
    assert login.status_code == 200, login.text
    client.headers.update({"X-CSRF-Token": login.json()["csrf"]})
    assert (
        client.post("/api/setup", json={"admin_password": "correct horse battery"}).status_code
        == 200
    )
    login = client.post(
        "/api/login", json={"username": "admin", "password": "correct horse battery"}
    )
    assert login.status_code == 200, login.text
    client.headers.update({"X-CSRF-Token": login.json()["csrf"]})


def test_querylog_disabled_warning(settings, fake):
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        login_admin(client)
        reconciler = app.state.reconciler
        fake.ql_config["enabled"] = False
        reconciler.meter(int(time.time()) + 601)

        state = client.get("/api/state").json()
        assert "querylog_disabled" in state["warnings"]
        assert client.get("/healthz").status_code == 200

        fake.ql_config["interval"] = 600_000  # ten minutes of retention
        reconciler.meter(int(time.time()) + 1300)
        state = client.get("/api/state").json()
        assert "querylog_retention_short" in state["warnings"]


def test_querylog_ignores_group_warning(settings, fake):
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        login_admin(client)
        created = client.post(
            "/api/groups",
            json={"name": "Games", "weekday_minutes": 10, "domains_text": "games.example"},
        )
        assert created.status_code == 200, created.text

        reconciler = app.state.reconciler
        fake.ql_config["ignored"] = ["*.games.example"]
        reconciler.meter(int(time.time()) + 601)

        state = client.get("/api/state").json()
        assert "querylog_ignores_group" in state["warnings"]
