"""Usage summary and CSV export: one query function, two statements, safe cells."""

from __future__ import annotations

import csv
import io
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from timefence import budget, db
from timefence.api import create_app

from .conftest import FakeAdGuard

TZ = ZoneInfo("UTC")
BERLIN = ZoneInfo("Europe/Berlin")

ADMIN = {"username": "admin", "password": "admin"}
ADMIN2 = {"username": "admin", "password": "correct horse battery"}


@pytest.fixture()
def store(tmp_path):
    store = db.Store(tmp_path / "usage.db")
    with store as conn:
        db.migrate(conn, TZ)
    yield store
    store.close()


def ts_berlin(year, month, day, hour, minute=0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=BERLIN).timestamp())


def make_group(store, name="YouTube", *, minutes=100, weekend=None, domains=()):
    with store as conn:
        return budget.create_group(
            conn,
            name=name,
            emoji="",
            weekday_minutes=minutes,
            weekend_minutes=minutes if weekend is None else weekend,
            services=(),
            domains=list(domains) or [f"{name.lower().replace(' ', '-')}.example"],
        )


def add_usage(store, group, day: str, seconds: int, bonus: int = 0) -> None:
    with store as conn:
        budget.add_metered_seconds(conn, group.id, day, seconds, cap_seconds=None)
        if bonus:
            conn.execute(
                "INSERT INTO bonus(day, group_id, seconds) VALUES(?,?,?) "
                "ON CONFLICT(day, group_id) DO UPDATE SET seconds=excluded.seconds",
                (day, group.id, bonus),
            )
            conn.commit()


def raw_group(store, name: str) -> int:
    with store as conn:
        cur = conn.execute(
            "INSERT INTO groups(name, emoji, weekday_minutes, weekend_minutes, paused, deleted, "
            "created_at) VALUES(?,?,?,?,0,0,0)",
            (name, "", 60, 60),
        )
        conn.commit()
        return int(cur.lastrowid)


def sign_in(client, username: str, password: str) -> str:
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    csrf = response.json()["csrf"]
    client.headers.update({"X-CSRF-Token": csrf})
    return csrf


def complete_setup(client) -> None:
    sign_in(client, "admin", "admin")
    setup = client.post("/api/setup", json={"admin_password": ADMIN2["password"]})
    assert setup.status_code == 200, setup.text
    sign_in(client, "admin", ADMIN2["password"])


def make_app(settings):
    return create_app(settings, adguard=FakeAdGuard())


# --------------------------------------------------------------------------- #
# budget.usage_range
# --------------------------------------------------------------------------- #


def test_usage_range_follows_local_dates_across_a_dst_week(store):
    group = make_group(store, "Games", minutes=100, weekend=200)
    now = ts_berlin(2026, 10, 26, 12)  # the Monday after the October change
    add_usage(store, group, "2026-10-20", 600)
    add_usage(store, group, "2026-10-25", 1800)  # the 25-hour Sunday
    add_usage(store, group, "2026-10-19", 900)  # outside the window

    with store as conn:
        rows = budget.usage_range(conn, BERLIN, days=7, now=now)

    assert [row["day"] for row in rows] == [
        "2026-10-20",
        "2026-10-21",
        "2026-10-22",
        "2026-10-23",
        "2026-10-24",
        "2026-10-25",
        "2026-10-26",
    ]
    assert sum(row["used_seconds"] for row in rows) == 2400
    with store as conn:
        metered = sum(
            budget.metered_seconds(conn, group.id, day) for day in [row["day"] for row in rows]
        )
    assert sum(row["used_seconds"] for row in rows) == metered
    # the budget column uses the current minutes applied to that date
    by_day = {row["day"]: row for row in rows}
    assert by_day["2026-10-25"]["budget_minutes"] == 200  # a Sunday
    assert by_day["2026-10-26"]["budget_minutes"] == 100  # a Monday
    assert by_day["2026-10-25"]["used_minutes"] == 30.0


def test_usage_range_runs_exactly_two_queries(store):
    for index in range(5):
        make_group(store, f"Group {index}")
    statements: list[str] = []
    with store as conn:
        conn.set_trace_callback(statements.append)
        budget.usage_range(conn, TZ, days=30, now=int(time.time()))
        conn.set_trace_callback(None)
    assert len(statements) == 2, statements


def test_usage_range_zero_fills_and_skips_deleted_groups(store):
    kept = make_group(store, "Kept")
    gone = make_group(store, "Gone")
    add_usage(store, kept, budget.day_str(int(time.time()), TZ), 300, bonus=120)
    with store as conn:
        budget.delete_group(conn, gone.id)
        rows = budget.usage_range(conn, TZ, days=7, now=int(time.time()))
    assert {row["group"] for row in rows} == {"Kept"}
    today = rows[-1]
    assert today["group_id"] == kept.id
    assert today["used_seconds"] == 300
    assert today["used_minutes"] == 5.0
    assert today["bonus_seconds"] == 120
    assert today["bonus_minutes"] == 2.0
    assert rows[0]["used_seconds"] == 0


def test_usage_range_on_an_empty_database(store):
    with store as conn:
        assert budget.usage_range(conn, TZ, days=7) == []
        assert budget.usage_range(conn, TZ, days=30) == []


# --------------------------------------------------------------------------- #
# the HTTP endpoints
# --------------------------------------------------------------------------- #


def test_usage_endpoint_matches_the_query_and_is_admin_only(settings):
    app = make_app(settings)
    with TestClient(app) as client:
        complete_setup(client)
        created = client.post(
            "/api/groups",
            json={"name": "YouTube", "weekday_minutes": 100, "domains_text": "youtube.example"},
        )
        assert created.status_code == 200, created.text
        group_id = created.json()["group"]["id"]
        with app.state.store as conn:
            budget.add_metered_seconds(
                conn, group_id, budget.day_str(int(time.time()), app.state.settings.zone), 900
            )
        for days in ("7", "14", "30"):
            response = client.get(f"/api/usage?days={days}")
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"
            payload = response.json()
            assert payload["days"] == int(days)
            assert len(payload["rows"]) == int(days)
            with app.state.store as conn:
                expected = budget.usage_range(
                    conn, app.state.settings.zone, int(days), int(time.time())
                )
            assert payload["rows"] == expected

        for bad in ("abc", "0", "31", "7.5", ""):
            response = client.get(f"/api/usage?days={bad}")
            assert response.status_code == 400, (bad, response.text)
            assert response.json()["code"] == "invalid"
            assert response.json()["error"]

        kid = client.post(
            "/api/users", json={"username": "kid", "password": "kid-password", "role": "user"}
        )
        assert kid.status_code == 200, kid.text
        sign_in(client, "kid", "kid-password")
        assert client.get("/api/usage?days=7").status_code == 403
        assert client.get("/api/usage.csv?days=7").status_code == 403


def test_usage_csv_is_safe_utf8_and_fixed(settings):
    app = make_app(settings)
    names = [
        "=1+1",
        "-2+3",
        "@SUM(A1)",
        "\t=cmd",
        "\r=cmd",
        " =cmd",
        "a,b",
        'a"b',
        "a\nb",
        "Игры",
        "'quoted",
    ]
    with TestClient(app) as client:
        complete_setup(client)
        for name in names:
            raw_group(app.state.store, name)
        response = client.get("/api/usage.csv?days=7")
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/csv")
        assert response.headers["content-disposition"] == (
            'attachment; filename="timefence-usage.csv"'
        )
        assert response.headers["cache-control"] == "no-store"
        body = response.content
        assert not body.startswith(b"\xef\xbb\xbf")  # no BOM
        table = list(csv.reader(io.StringIO(body.decode("utf-8"))))
        assert table[0] == ["group", "date", "used_minutes", "bonus_minutes"]
        assert all(len(row) == 4 for row in table)
        exported = {row[0] for row in table[1:]}
        assert "'=1+1" in exported
        assert "'-2+3" in exported
        assert "'@SUM(A1)" in exported
        assert "'=cmd" in exported  # from the tab, the CR and the leading space
        assert "a,b" in exported
        assert 'a"b' in exported
        assert "ab" in exported
        assert "Игры" in exported
        assert "''quoted" in exported
        assert "budget_minutes" not in table[0]

        for bad in ("abc", "0", "31"):
            assert client.get(f"/api/usage.csv?days={bad}").status_code == 400


def test_usage_csv_rows_follow_the_json_rows(settings):
    app = make_app(settings)
    with TestClient(app) as client:
        complete_setup(client)
        created = client.post(
            "/api/groups",
            json={"name": "Games", "weekday_minutes": 30, "domains_text": "games.example"},
        )
        assert created.status_code == 200, created.text
        payload = client.get("/api/usage?days=7").json()
        body = client.get("/api/usage.csv?days=7").content.decode("utf-8")
        table = list(csv.reader(io.StringIO(body)))[1:]
        assert len(table) == len(payload["rows"])
        assert [row[1] for row in table] == [row["day"] for row in payload["rows"]]
        assert [row[0] for row in table] == [row["group"] for row in payload["rows"]]
        assert [float(row[2]) for row in table] == [row["used_minutes"] for row in payload["rows"]]


def test_usage_csv_handles_an_empty_database(settings):
    app = make_app(settings)
    with TestClient(app) as client:
        complete_setup(client)
        body = client.get("/api/usage.csv?days=7").content.decode("utf-8")
        rows = list(csv.reader(io.StringIO(body)))
    assert rows == [["group", "date", "used_minutes", "bonus_minutes"]]
