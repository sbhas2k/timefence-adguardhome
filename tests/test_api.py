"""HTTP flows: users, forced setup, CSRF, groups, modes, guard, sessions, fail-safe."""

from __future__ import annotations

from fastapi.testclient import TestClient

from timefence.api import create_app

from .conftest import FakeAdGuard, make_entry

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
    payload = {"name": "Games", "weekday_minutes": 45, "services": ["roblox"], "mode": "session"}
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
    response = client.post("/api/session/stop", json={"group_id": 1}, headers={"X-CSRF-Token": "x"})
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
    assert group["mode"] == "auto"
    assert group["permitted"] is True
    assert fake.ids == []  # nothing blocked: ads only
    assert fake.rules == []


def test_session_mode_is_blocked_by_default(ready, fake):
    client = ready["client"]
    group = make_group(client, services=["youtube"])
    assert group["permitted"] is False
    assert fake.ids == ["youtube"]

    client.put(
        f"/api/groups/{group['id']}",
        json={
            "name": group["name"],
            "weekday_minutes": 60,
            "services": ["youtube"],
            "mode": "auto",
        },
    )
    assert fake.ids == []  # switched to auto -> unblocked


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
            "mode": "session",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["group"]["weekend_minutes"] == 45

    assert client.post(f"/api/groups/{group['id']}/pause", json={"paused": True}).status_code == 200
    assert (
        client.post("/api/session/start", json={"group_id": group["id"], "minutes": 15}).status_code
        == 409
    )  # paused

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
            "mode": "session",
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["group"]["domains"] == ["example-game.com"]
    assert "||example-game.com^" in fake.rules

    second = client.post(
        "/api/groups",
        json={"name": "Other", "weekday_minutes": 45, "domains_text": "example-game.com"},
    )
    assert second.status_code == 400
    assert "already used" in second.json()["error"]


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
    results = client.get(f"/api/groups/{group['id']}/test").json()["results"]
    hosts = {r["host"]: r["blocked"] for r in results}
    assert hosts["roblox.com"] is True


# --------------------------------------------------------------------------- #
# sessions (session mode) and fail-safety
# --------------------------------------------------------------------------- #


def test_session_start_unblocks_then_blocks_at_expiry(ready, fake):
    client = ready["client"]
    group = make_group(
        client, weekday_minutes=2, services=["roblox"], domains_text="example-game-test.com"
    )
    assert fake.ids == ["roblox"]
    assert "||example-game-test.com^" in fake.rules

    started = client.post("/api/session/start", json={"group_id": group["id"], "minutes": 60})
    assert started.status_code == 200, started.text
    assert started.json()["session"]["planned_seconds"] == 120
    assert fake.ids == []
    assert fake.rules == []

    conn = client.app.state.conn
    conn.execute("UPDATE sessions SET started_at=started_at-180 WHERE ended_at IS NULL")
    conn.commit()
    client.app.state.reconciler.run("test")
    assert fake.ids == ["roblox"]
    assert "||example-game-test.com^" in fake.rules
    entry = client.get("/api/state").json()["groups"][0]
    assert entry["remaining_minutes"] <= 0.5
    assert entry["active_session"] is None


def test_stop_bills_only_used_minutes(ready, fake):
    client = ready["client"]
    group = make_group(client, weekday_minutes=60)
    client.post("/api/session/start", json={"group_id": group["id"], "minutes": 15})
    conn = client.app.state.conn
    conn.execute("UPDATE sessions SET started_at=started_at-90 WHERE ended_at IS NULL")
    conn.commit()
    stopped = client.post("/api/session/stop", json={"group_id": group["id"]}).json()
    assert stopped["stopped"][0]["used_seconds"] >= 90
    assert fake.ids == ["roblox"]


def test_fail_safe_restart_blocks_everything(settings, fake):
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        complete_setup(client)
        group = make_group(client, weekday_minutes=60)
        client.post("/api/session/start", json={"group_id": group["id"], "minutes": 15})
        assert fake.ids == []

    fake2 = FakeAdGuard()
    app2 = create_app(settings, adguard=fake2)
    with TestClient(app2) as client2:
        rows = list(client2.app.state.conn.execute("SELECT end_reason FROM sessions"))
        assert rows and all(row["end_reason"] == "service_restart" for row in rows)
        assert fake2.ids == ["roblox"]


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


def test_groups_can_be_owned_and_users_only_see_their_own(ready, fake):
    client = ready["client"]
    kid1 = client.post(
        "/api/users", json={"username": "kid1", "password": "kid1-password", "role": "user"}
    ).json()["user"]
    kid2 = client.post(
        "/api/users", json={"username": "kid2", "password": "kid2-password", "role": "user"}
    ).json()["user"]

    own = client.post(
        "/api/groups",
        json={
            "name": "Kid1 games",
            "weekday_minutes": 20,
            "services": ["roblox"],
            "mode": "session",
            "owner_id": kid1["id"],
        },
    ).json()["group"]
    assert own["owner"] == "kid1"

    other = client.post(
        "/api/groups",
        json={
            "name": "Kid2 games",
            "weekday_minutes": 20,
            "services": ["youtube"],
            "mode": "session",
            "owner_id": kid2["id"],
        },
    ).json()["group"]
    shared = client.post(
        "/api/groups", json={"name": "Shared", "weekday_minutes": 10, "services": ["instagram"]}
    ).json()["group"]
    assert shared["owner"] is None

    # admin sees everything
    admin_names = {g["name"] for g in client.get("/api/state").json()["groups"]}
    assert admin_names == {"Kid1 games", "Kid2 games", "Shared"}

    # kid1 sees their own + shared only
    assert login(client, "kid1", "kid1-password").status_code == 200
    kid_state = client.get("/api/state").json()
    assert {g["name"] for g in kid_state["groups"]} == {"Kid1 games", "Shared"}

    # and cannot touch kid2's group
    assert (
        client.post("/api/session/start", json={"group_id": other["id"], "minutes": 15}).status_code
        == 404
    )
    assert (
        client.post("/api/session/start", json={"group_id": own["id"], "minutes": 15}).status_code
        == 200
    )

    # admin can reassign a group (log back in as admin first)
    login(client, "admin", "correct horse battery")
    moved = client.put(
        f"/api/groups/{own['id']}",
        json={
            "name": "Kid1 games",
            "weekday_minutes": 20,
            "services": ["roblox"],
            "mode": "session",
            "owner_id": kid2["id"],
        },
    )
    assert moved.status_code == 200
    assert moved.json()["group"]["owner"] == "kid2"


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
    conn = client.app.state.conn
    from timefence import budget

    now = int(__import__("time").time())
    day = budget.day_str(now, client.app.state.settings.zone)
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
