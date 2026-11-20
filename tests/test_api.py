"""HTTP flows: users, forced setup, CSRF, groups, guard, fail-safe."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from timefence.api import create_app

from .conftest import FakeAdGuard, locked_conn, make_entry
from .fakes import StubServer

ADMIN = {"username": "admin", "password": "admin"}
ADMIN2 = {"username": "admin", "password": "correct horse battery"}


def login(client, username, password):
    response = client.post("/api/login", json={"username": username, "password": password})
    if response.status_code == 200:
        client.headers.update({"X-CSRF-Token": response.json()["csrf"]})
    return response


def complete_setup(client):
    login(client, **ADMIN)
    assert client.post("/api/setup", json={"admin_password": ADMIN2["password"]}).status_code == 200
    login(client, **ADMIN2)
    return client


def make_group(client, **overrides):
    payload = {"name": "Games", "weekday_minutes": 45, "services": ["roblox"]}
    payload.update(overrides)
    response = client.post("/api/groups", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["group"]


# --------------------------------------------------------------------------- #
# first-run setup and login
# --------------------------------------------------------------------------- #


def test_before_setup_only_login_and_setup_work(client):
    assert client.get("/api/state").status_code == 403
    assert (
        client.post("/api/login", json={"username": "nobody", "password": "x"}).status_code == 401
    )
    response = login(client, **ADMIN)
    assert response.status_code == 200
    assert response.json()["role"] == "admin"
    assert client.get("/api/meta").json()["setup_complete"] is False
    assert client.get("/api/state").status_code == 403  # still gated


def test_setup_validates_and_completes(admin):
    client = admin["client"]
    assert client.post("/api/setup", json={"admin_password": "admin"}).status_code == 400
    assert client.post("/api/setup", json={"admin_password": "short"}).status_code == 400
    assert client.post("/api/setup", json={"admin_password": ADMIN2["password"]}).status_code == 200
    assert client.get("/api/meta").json()["setup_complete"] is True
    assert client.post("/api/login", json=ADMIN).status_code == 401
    assert login(client, **ADMIN2).status_code == 200
    assert client.get("/api/state").status_code == 200


def test_csrf_is_required_for_writes(ready):
    client = ready["client"]
    response = client.post("/api/groups/1/block", json={}, headers={"X-CSRF-Token": "x"})
    assert response.status_code == 403
    assert response.json()["code"] == "csrf"


def test_login_rate_limit_locks_the_client(settings):
    app = create_app(settings, adguard=FakeAdGuard())
    with TestClient(app) as client:
        complete_setup(client)
        for _ in range(4):
            assert login(client, "admin", "nope").status_code == 401
        assert login(client, "admin", "nope").status_code == 429
        assert login(client, **ADMIN2).status_code == 429


# --------------------------------------------------------------------------- #
# users
# --------------------------------------------------------------------------- #


def test_user_crud_and_guards(ready):
    client = ready["client"]
    created = client.post(
        "/api/users", json={"username": "kid", "password": "kid-password", "role": "user"}
    )
    assert created.status_code == 200, created.text
    kid = created.json()["user"]
    assert kid["role"] == "user"

    assert (
        client.post(
            "/api/users", json={"username": "kid", "password": "x" * 10, "role": "user"}
        ).status_code
        == 409
    )
    assert (
        client.post("/api/users", json={"username": "bad name", "password": "x" * 10}).status_code
        == 400
    )
    assert (
        client.post("/api/users", json={"username": "shortpw", "password": "123"}).status_code
        == 400
    )

    users = client.get("/api/users").json()["users"]
    assert {u["username"] for u in users} == {"admin", "kid"}

    # promote + demote
    assert client.post(f"/api/users/{kid['id']}/role", json={"role": "admin"}).status_code == 200
    assert client.post(f"/api/users/{kid['id']}/role", json={"role": "user"}).status_code == 200

    # can't demote/disable/delete the last admin (only admin is 'admin' and it is ourselves)
    me = [u for u in users if u["username"] == "admin"][0]
    assert client.post(f"/api/users/{me['id']}/role", json={"role": "user"}).status_code == 409
    assert (
        client.post(f"/api/users/{me['id']}/disabled", json={"disabled": True}).status_code == 409
    )
    assert client.delete(f"/api/users/{me['id']}").status_code == 409

    # disable the kid, delete it
    assert (
        client.post(f"/api/users/{kid['id']}/disabled", json={"disabled": True}).status_code == 200
    )
    assert login(client, "kid", "kid-password").status_code == 401
    assert client.delete(f"/api/users/{kid['id']}").status_code == 200
    assert [u["username"] for u in client.get("/api/users").json()["users"]] == ["admin"]


def test_normal_user_cannot_use_admin_endpoints(ready):
    client = ready["client"]
    client.post("/api/users", json={"username": "kid", "password": "kid-password", "role": "user"})
    user = login(client, "kid", "kid-password")
    assert user.status_code == 200
    headers = {"X-CSRF-Token": user.json()["csrf"]}
    assert client.get("/api/state", headers=headers).status_code == 200
    assert client.get("/api/groups", headers=headers).status_code == 200
    assert client.get("/api/users", headers=headers).status_code == 403
    assert (
        client.post(
            "/api/groups",
            json={"name": "x", "weekday_minutes": 1, "domains_text": "x.com"},
            headers=headers,
        ).status_code
        == 403
    )


# --------------------------------------------------------------------------- #
# groups and modes
# --------------------------------------------------------------------------- #


def test_auto_mode_is_allowed_by_default(ready, fake):
    client = ready["client"]
    group = client.post(
        "/api/groups", json={"name": "YouTube", "weekday_minutes": 60, "services": ["youtube"]}
    ).json()["group"]
    assert group["permitted"] is True
    assert fake.ids == []  # nothing blocked: ads only
    assert fake.rules == []


def test_session_mode_is_rejected(ready):
    client = ready["client"]
    created = client.post(
        "/api/groups",
        json={"name": "YouTube", "weekday_minutes": 60, "services": ["youtube"], "mode": "session"},
    )
    assert created.status_code == 400
    assert created.json()["code"] == "mode_removed"

    group = client.post(
        "/api/groups", json={"name": "YouTube", "weekday_minutes": 60, "services": ["youtube"]}
    ).json()["group"]
    updated = client.put(
        f"/api/groups/{group['id']}",
        json={"name": "YouTube", "weekday_minutes": 30, "mode": "session"},
    )
    assert updated.status_code == 400
    assert updated.json()["code"] == "mode_removed"


def test_session_endpoints_are_gone(ready):
    client = ready["client"]
    group = make_group(client)
    started = client.post("/api/session/start", json={"group_id": group["id"], "minutes": 15})
    stopped = client.post("/api/session/stop", json={"group_id": group["id"]})
    assert started.status_code in {404, 405}
    assert stopped.status_code in {404, 405}


def test_state_has_no_session_fields(ready):
    client = ready["client"]
    make_group(client)
    state = client.get("/api/state").json()
    assert "sessions_today" not in state
    assert "session_options" not in state["app"]
    for group in state["groups"]:
        assert "active_session" not in group
        assert "mode" not in group
    assert "session_options" not in client.get("/api/meta").json()


def test_group_crud(ready, fake):
    client = ready["client"]
    group = make_group(client, name="YouTube", emoji="▶️", weekday_minutes=60, services=["youtube"])
    updated = client.put(
        f"/api/groups/{group['id']}",
        json={
            "name": "YouTube",
            "weekday_minutes": 30,
            "weekend_minutes": 45,
            "services": ["youtube"],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["group"]["weekend_minutes"] == 45

    assert fake.ids == []  # allowed until the daily minutes are used
    assert client.post(f"/api/groups/{group['id']}/pause", json={"paused": True}).status_code == 200
    assert fake.ids == ["youtube"]

    assert client.delete(f"/api/groups/{group['id']}").status_code == 200
    assert fake.ids == []


def test_domains_are_normalised_and_unique(ready, fake):
    client = ready["client"]
    first = client.post(
        "/api/groups",
        json={
            "name": "Games",
            "weekday_minutes": 45,
            "domains_text": "https://Example-Game.com/play?x=1",
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["group"]["domains"] == ["example-game.com"]
    assert fake.rules == []  # allowed, so nothing is blocked yet

    client.post(f"/api/groups/{first.json()['group']['id']}/block", json={})
    assert "||example-game.com^" in fake.rules

    second = client.post(
        "/api/groups",
        json={"name": "Other", "weekday_minutes": 45, "domains_text": "example-game.com"},
    )
    assert second.status_code == 400
    assert "already used" in second.json()["error"]


def test_group_windows_round_trip_and_validation(ready, fake):
    client = ready["client"]
    windows = [{"start": "21:00", "end": "07:00", "days": "all"}]

    group = make_group(client, windows=windows)
    assert group["windows"] == windows
    assert client.get("/api/state").json()["groups"][0]["windows"] == windows

    # an update that omits the key keeps the windows, an empty list clears them
    kept = client.put(f"/api/groups/{group['id']}", json={"name": "Games", "services": ["roblox"]})
    assert kept.status_code == 200, kept.text
    assert kept.json()["group"]["windows"] == windows
    cleared = client.put(
        f"/api/groups/{group['id']}",
        json={"name": "Games", "services": ["roblox"], "windows": []},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["group"]["windows"] == []

    def reject(payload):
        response = client.post(
            "/api/groups",
            json={"name": "Other", "weekday_minutes": 30, "services": ["roblox"], **payload},
        )
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "invalid_windows"

    reject({"windows": [{"start": "21:00", "end": "21:00", "days": "all"}]})
    reject({"windows": [{"start": "9:00", "end": "10:00", "days": "all"}]})
    reject({"windows": [{"start": "09:00", "end": "10:00", "days": "daily"}]})
    reject(
        {
            "windows": [
                {"start": "06:00", "end": "07:00", "days": "all"},
                {"start": "12:00", "end": "13:00", "days": "all"},
                {"start": "15:00", "end": "16:00", "days": "all"},
                {"start": "18:00", "end": "19:00", "days": "all"},
                {"start": "21:00", "end": "22:00", "days": "all"},
            ]
        }
    )


def test_status_and_permitted_agree(ready, fake, monkeypatch):
    client = ready["client"]
    # frozen at local noon: the window and the session are both minted against
    # this instant, so the result does not depend on when the suite runs
    fixed = datetime(2026, 11, 11, 12, 0, tzinfo=UTC).timestamp()
    monkeypatch.setattr(time, "time", lambda: fixed)
    login = client.post("/api/login", json=ADMIN2)
    assert login.status_code == 200, login.text
    client.headers.update({"X-CSRF-Token": login.json()["csrf"]})
    start, end = "11:55", "12:55"
    group = make_group(client, windows=[{"start": start, "end": end, "days": "all"}])

    state = client.get("/api/state").json()["groups"][0]
    listed = client.get("/api/groups").json()["groups"][0]
    assert state["status"] == "bedtime"
    assert state["permitted"] is False
    assert state["permitted"] == (state["status"] in {"allowed", "free"})
    assert state["bedtime_active"] is True
    assert state["bedtime_until"] == end
    assert (listed["status"], listed["permitted"]) == (state["status"], state["permitted"])

    created = client.post(
        "/api/users", json={"username": "kid", "password": "kid-password", "role": "user"}
    )
    assert created.status_code == 200, created.text
    login = client.post("/api/login", json={"username": "kid", "password": "kid-password"})
    assert login.status_code == 200, login.text
    kid_total = {"X-CSRF-Token": login.json()["csrf"]}
    kid_state = client.get("/api/state", headers=kid_total).json()["groups"][0]
    assert (kid_state["status"], kid_state["permitted"]) == (state["status"], state["permitted"])
    assert kid_state["bedtime_until"] == end
    assert group["windows"] == [{"start": start, "end": end, "days": "all"}]


def test_critical_domain_needs_confirmation(ready):
    client = ready["client"]
    response = client.post(
        "/api/groups",
        json={"name": "AppStore", "weekday_minutes": 10, "domains_text": "apps.apple.com"},
    )
    assert response.status_code == 409
    payload = response.json()
    assert payload["code"] == "critical_domains"
    assert payload["critical"][0]["domain"] == "apps.apple.com"
    assert "App Store" in payload["critical"][0]["note"]

    confirmed = client.post(
        "/api/groups",
        json={
            "name": "AppStore",
            "weekday_minutes": 10,
            "domains_text": "apps.apple.com",
            "confirm_critical": True,
        },
    )
    assert confirmed.status_code == 200


def test_discover_hides_noise_and_managed_domains(ready, fake):
    client = ready["client"]
    client.post(
        "/api/groups", json={"name": "Games", "weekday_minutes": 45, "domains_text": "known.com"}
    )
    client.post(
        "/api/groups", json={"name": "Roblox", "weekday_minutes": 45, "services": ["roblox"]}
    )
    fake.entries = [
        make_entry("api.example-game.com"),
        make_entry("ssl.google-analytics.com"),
        make_entry("already.known.com"),
        make_entry("www.youtube.com"),
        make_entry("assetdelivery.roblox.com"),
        make_entry("cdn.other-site.net"),
    ]
    data = client.get("/api/discover?minutes=15").json()
    names = [d["domain"] for d in data["domains"]]
    assert "example-game.com" in names
    assert "other-site.net" in names
    assert "google-analytics.com" not in names
    assert "known.com" not in names
    assert "roblox.com" not in names
    assert "youtube.com" in names

    all_data = client.get("/api/discover?minutes=15&show_all=true").json()
    assert "google-analytics.com" in [d["domain"] for d in all_data["domains"]]


def test_test_endpoint_reports_blocked_hosts(ready, fake):
    client = ready["client"]
    group = make_group(client, services=["roblox"])
    client.post(f"/api/groups/{group['id']}/block", json={})
    results = client.get(f"/api/groups/{group['id']}/test").json()["results"]
    hosts = {r["host"]: r["blocked"] for r in results}
    assert hosts["roblox.com"] is True


# --------------------------------------------------------------------------- #
# quick actions
# --------------------------------------------------------------------------- #


def test_block_now_sets_a_force_block(ready, fake):
    client = ready["client"]
    group = client.post(
        "/api/groups", json={"name": "YouTube", "weekday_minutes": 60, "services": ["youtube"]}
    ).json()["group"]
    assert fake.ids == []  # auto mode: allowed
    client.post(f"/api/groups/{group['id']}/block", json={})
    assert fake.ids == ["youtube"]
    state = client.get("/api/state").json()["groups"][0]
    assert state["force_block_until"] > 0
    assert state["permitted"] is False

    client.post(f"/api/groups/{group['id']}/force/clear", json={})
    assert fake.ids == []
    assert client.get("/api/state").json()["groups"][0]["permitted"] is True


def test_healthz_and_meta_are_public(client):
    assert client.get("/healthz").json()["ok"] is True
    meta = client.get("/api/meta").json()
    assert meta["version"]
    assert "unofficial" in meta


# --------------------------------------------------------------------------- #
# per-account budgets, bonus adjustments, reset
# --------------------------------------------------------------------------- #


def test_negative_bonus_reduces_the_budget(ready, fake):
    client = ready["client"]
    group = client.post(
        "/api/groups", json={"name": "YouTube", "weekday_minutes": 30, "services": ["youtube"]}
    ).json()["group"]
    assert group["remaining_minutes"] == 30

    assert client.post(f"/api/groups/{group['id']}/bonus", json={"minutes": -10}).status_code == 200
    state = client.get("/api/state").json()["groups"][0]
    assert state["bonus_minutes"] == -10
    assert state["remaining_minutes"] == 20
    assert fake.ids == []  # still allowed

    # minus the rest -> blocked
    client.post(f"/api/groups/{group['id']}/bonus", json={"minutes": -20})
    assert client.get("/api/state").json()["groups"][0]["permitted"] is False
    assert fake.ids == ["youtube"]


def test_reset_today_clears_metered_usage(ready, fake):
    client = ready["client"]
    group = client.post(
        "/api/groups", json={"name": "YouTube", "weekday_minutes": 10, "services": ["youtube"]}
    ).json()["group"]
    from timefence import budget

    now = int(__import__("time").time())
    day = budget.day_str(now, client.app.state.settings.zone)
    with locked_conn(client.app) as conn:
        budget.add_bonus(conn, group["id"], 15, client.app.state.settings.zone)  # 10 + 15 min
        budget.add_metered_seconds(conn, group["id"], day, 1500)  # use it all
    client.app.state.reconciler.run("test")
    assert client.get("/api/state").json()["groups"][0]["permitted"] is False
    assert fake.ids == ["youtube"]

    assert client.post(f"/api/groups/{group['id']}/reset", json={}).status_code == 200
    state = client.get("/api/state").json()["groups"][0]
    assert state["used_minutes"] == 0
    assert state["bonus_minutes"] == 0
    assert state["remaining_minutes"] == 10
    assert state["permitted"] is True
    assert fake.ids == []


def test_block_now_lasts_until_local_midnight_on_dst_day(tmp_path, fake, monkeypatch):
    import types
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from timefence import api as api_module
    from timefence import budget as budget_module
    from timefence.config import Settings

    ny = ZoneInfo("America/New_York")
    fixed = int(datetime(2026, 11, 1, 12, 0, tzinfo=ny).timestamp())
    fake_time = types.SimpleNamespace(time=lambda: fixed)
    monkeypatch.setattr(api_module, "time", fake_time)
    monkeypatch.setattr(budget_module, "time", fake_time)

    settings = Settings.from_env(
        {
            "ADGUARD_URL": "http://127.0.0.1:3000",
            "ADGUARD_USER": "admin",
            "ADGUARD_PASSWORD": "secret",
            "BASE_PATH": "/",
            "TZ": "America/New_York",
            "DATA_DIR": str(tmp_path / "data"),
            "SECURE_COOKIES": "false",
            "RECONCILE_INTERVAL": "60",
            "LOG_LEVEL": "WARNING",
        }
    )
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        complete_setup(client)
        created = client.post(
            "/api/groups",
            json={"name": "YouTube", "weekday_minutes": 60, "services": ["youtube"]},
        )
        assert created.status_code == 200, created.text
        group_id = created.json()["group"]["id"]
        assert client.post(f"/api/groups/{group_id}/block", json={}).status_code == 200
        groups = client.get("/api/state").json()["groups"]

    blocked_until = [row for row in groups if row["id"] == group_id][0]["force_block_until"]
    expected = int(datetime(2026, 11, 2, 0, 0, tzinfo=ny).timestamp())
    assert blocked_until == expected


# --------------------------------------------------------------------------- #
# alerts
# --------------------------------------------------------------------------- #


def test_alert_config_is_masked_merged_and_validated(ready):
    client = ready["client"]
    response = client.get("/api/alerts")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    shown = response.json()
    assert shown["channel"] == ""
    assert shown["enabled"] is True
    assert shown["events"]["bedtime_started"] is False
    assert shown["events"]["weekly_summary"] is False
    assert shown["events"]["group_low"] is True

    topic = "topic-SUPERSECRET"
    token = "token-THIS-MUST-NOT-LEAK"
    hook = "http://192.168.1.9:8080/private/hook-SECRET"
    update = client.put(
        "/api/alerts",
        json={
            "channel": "ntfy",
            "ntfy_topic": topic,
            "ntfy_token": token,
            "webhook_url": hook,
            "events": {"weekly_summary": True},
        },
    )
    assert update.status_code == 200, update.text
    assert update.headers["cache-control"] == "no-store"
    shown = update.json()
    text = json.dumps(shown)
    for secret in (topic, token, hook, "/private/hook"):
        assert secret not in text
    assert shown["ntfy"]["topic_tail"] == topic[-2:]
    assert shown["ntfy"]["token_configured"] is True
    assert shown["webhook"]["path_tail"] == "ET"
    assert shown["events"]["weekly_summary"] is True

    body = client.get("/api/alerts").text
    assert topic not in body and token not in body

    # an absent key keeps the stored value, "" clears it
    kept = client.put("/api/alerts", json={"events": {"group_low": False}}).json()
    assert kept["channel"] == "ntfy"
    assert kept["ntfy"]["topic_tail"] == topic[-2:]
    assert kept["events"]["group_low"] is False
    cleared = client.put("/api/alerts", json={"ntfy_token": ""}).json()
    assert cleared["ntfy"]["token_configured"] is False

    # switching the channel keeps the other channel's stored values
    switched = client.put("/api/alerts", json={"channel": "webhook"}).json()
    assert switched["channel"] == "webhook"
    assert switched["ntfy"]["topic_tail"] == topic[-2:]

    # config is never written to the event log
    with locked_conn(client.app) as conn:
        details = [row["detail"] or "" for row in conn.execute("SELECT detail FROM events")]
    assert all("SECRET" not in detail and "TOKEN" not in detail for detail in details)

    for bad in (
        {"channel": "email"},
        {"ntfy_topic": "bad topic!"},
        {"ntfy_base": "ftp://ntfy.example"},
        {"ntfy_token": "token with a space"},
        {"webhook_url": "http://u:p@host/hook"},
        {"events": []},
        {"events": {"group_low": "maybe"}},
    ):
        response = client.put("/api/alerts", json=bad)
        assert response.status_code == 400, (bad, response.text)


def test_alert_test_endpoint_reports_categories(ready, deliveries):
    from timefence import alerts

    client = ready["client"]
    assert client.post("/api/alerts/test", json={}).json() == {"ok": False, "category": "config"}

    assert alerts.TEST_LOCK.acquire(blocking=False)
    try:
        busy = client.post("/api/alerts/test", json={})
    finally:
        alerts.TEST_LOCK.release()
    assert busy.status_code == 429
    assert busy.json()["code"] == "busy"

    with StubServer(status=500) as stub:
        client.put("/api/alerts", json={"channel": "webhook", "webhook_url": stub.url + "/hook"})
        assert client.post("/api/alerts/test", json={}).json() == {
            "ok": False,
            "category": "http_500",
        }

    with StubServer(status=302) as stub:
        stub.location = stub.url + "/elsewhere"
        client.put("/api/alerts", json={"channel": "webhook", "webhook_url": stub.url + "/hook"})
        assert client.post("/api/alerts/test", json={}).json() == {
            "ok": False,
            "category": "http_3xx",
        }
        assert len(stub.requests) == 1

    with StubServer() as stub:
        client.put("/api/alerts", json={"channel": "webhook", "webhook_url": stub.url + "/hook"})
        answer = client.post("/api/alerts/test", json={})
    assert answer.status_code == 200
    assert answer.json() == {"ok": True}
    assert len(stub.requests) == 1
    # the test result is remembered without any body or path
    shown = client.get("/api/alerts").json()
    assert shown["last_result"] == "ok"
    assert shown["last_result_ts"]


# --------------------------------------------------------------------------- #
# input hygiene: ownership on delete, payload shapes, icons, logout, body cap
# --------------------------------------------------------------------------- #


def test_deleting_a_user_leaves_their_groups_shared(ready):
    client = ready["client"]
    kid = client.post(
        "/api/users", json={"username": "kid", "password": "kid-password", "role": "user"}
    ).json()["user"]
    kept = make_group(client, name="Kept", owner_id=kid["id"])
    gone = make_group(client, name="Gone", owner_id=kid["id"], services=["instagram"])
    assert client.delete(f"/api/groups/{gone['id']}").status_code == 200

    assert client.delete(f"/api/users/{kid['id']}").status_code == 200

    groups = {row["id"]: row for row in client.get("/api/state").json()["groups"]}
    assert groups[kept["id"]]["owner_id"] is None
    assert groups[kept["id"]]["name"] == "Kept"
    # the soft-deleted row must not keep a dangling owner either
    with locked_conn(client.app) as conn:
        owners = {row["owner_id"] for row in conn.execute("SELECT owner_id FROM groups")}
    assert owners == {None}


def test_group_payload_types_are_checked(ready):
    client = ready["client"]
    bad_services = client.post(
        "/api/groups", json={"name": "X", "weekday_minutes": 30, "services": 5}
    )
    assert bad_services.status_code == 400
    assert bad_services.json()["code"] == "invalid"
    bad_domains = client.post(
        "/api/groups", json={"name": "X", "weekday_minutes": 30, "domains": "example.com"}
    )
    assert bad_domains.status_code == 400
    assert bad_domains.json()["code"] == "invalid"

    group = make_group(client)
    too_long = client.put(
        f"/api/groups/{group['id']}", json={"name": "x" * 500, "services": ["roblox"]}
    )
    assert too_long.status_code == 400
    assert too_long.json()["code"] == "invalid"
    fresh = client.get("/api/state").json()["groups"][0]
    assert fresh["name"] == "Games"


def test_service_icons_are_sanitised(ready, fake):
    from timefence import api

    assert api.safe_icon("PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciLz4=")
    assert api.safe_icon('x" onerror="alert(1)') == ""
    assert api.safe_icon(None) == ""
    assert api.safe_icon("") == ""

    fake.SERVICES = fake.SERVICES + [
        {"id": "evil", "name": "Evil", "rules": [], "icon_svg": 'x" onerror="alert(1)'},
        {"id": "good", "name": "Good", "rules": [], "icon_svg": "PHN2Zy8+"},
    ]
    services = {row["id"]: row for row in ready["client"].get("/api/services").json()["services"]}
    assert services["evil"]["icon_svg"] == ""
    assert services["good"]["icon_svg"] == "PHN2Zy8+"


def test_logout_revokes_the_copied_cookie(ready):
    client = ready["client"]
    session = client.cookies.get("timefence_session")
    assert session

    assert client.post("/api/logout", json={}).status_code == 200
    assert client.get("/api/state").status_code == 401

    copied = TestClient(client.app)
    copied.cookies.set("timefence_session", session, path="/")
    assert copied.get("/api/state").status_code == 401
    copied.close()


def test_an_oversized_login_body_is_rejected(ready):
    client = ready["client"]
    response = client.post("/api/login", json={"username": "admin", "password": "x" * (17 * 1024)})
    assert response.status_code == 413
    assert client.get("/api/state").status_code == 200


def test_large_service_icons_are_kept(settings):
    from .fakes import FaithfulAdGuard

    app = create_app(settings, adguard=FaithfulAdGuard())
    with TestClient(app) as client:
        complete_setup(client)
        services = {row["id"]: row for row in client.get("/api/services").json()["services"]}

    assert len(services["peacock_tv"]["icon_svg"]) == 9912
    assert len(services["imgur"]["icon_svg"]) == 8072
    assert services["youtube"]["icon_svg"]


def test_the_group_test_probes_a_representative_service_host(settings):
    from .fakes import FaithfulAdGuard

    app = create_app(settings, adguard=FaithfulAdGuard())
    with TestClient(app) as client:
        complete_setup(client)
        group = client.post(
            "/api/groups",
            json={"name": "YouTube", "weekday_minutes": 30, "services": ["youtube"]},
        ).json()["group"]
        results = client.get(f"/api/groups/{group['id']}/test").json()["results"]

    hosts = [row["host"] for row in results]
    assert "youtube.com" in hosts
    assert len(hosts) == 2


def test_a_rewritten_answer_is_not_reported_as_blocked(settings):
    from .fakes import FaithfulAdGuard

    fake = FaithfulAdGuard()
    fake.reasons = {
        "safesearch.example": "FilteredSafeSearch",
        "rewritten.example": "Rewrite",
        "blocked.example": "FilteredBlackList",
    }
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        complete_setup(client)
        group = client.post(
            "/api/groups",
            json={
                "name": "Mixed",
                "weekday_minutes": 30,
                "domains_text": "safesearch.example\nrewritten.example\nblocked.example",
            },
        ).json()["group"]
        results = {
            row["host"]: row
            for row in client.get(f"/api/groups/{group['id']}/test").json()["results"]
        }

    assert results["safesearch.example"]["blocked"] is False
    assert results["rewritten.example"]["blocked"] is False
    assert results["blocked.example"]["blocked"] is True
