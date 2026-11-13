"""HTTP flows: forced setup, CSRF, groups, guard, sessions, fail-safe, limiting."""

from __future__ import annotations

from fastapi.testclient import TestClient

from timefence.api import create_app

from .conftest import FakeAdGuard, make_entry


def complete_setup(client):
    client.post("/api/login", json={"role": "admin", "secret": "admin"})
    csrf = client.post("/api/login", json={"role": "admin", "secret": "admin"}).json()["csrf"]
    client.headers.update({"X-CSRF-Token": csrf})
    response = client.post(
        "/api/setup", json={"admin_password": "correct horse battery", "user_pin": "2468"}
    )
    assert response.status_code == 200, response.text
    response = client.post("/api/login", json={"role": "admin", "secret": "correct horse battery"})
    assert response.status_code == 200
    client.headers.update({"X-CSRF-Token": response.json()["csrf"]})
    return client


# --------------------------------------------------------------------------- #
# forced first-run setup
# --------------------------------------------------------------------------- #


def test_before_setup_everything_but_login_is_blocked(client):
    assert client.get("/api/state").status_code == 403
    assert client.get("/api/groups").status_code == 403
    # the user PIN is disabled until setup is done
    response = client.post("/api/login", json={"role": "user", "secret": "0000"})
    assert response.status_code == 403
    assert response.json()["code"] == "setup_required"
    # admin can log in with the documented default
    response = client.post("/api/login", json={"role": "admin", "secret": "admin"})
    assert response.status_code == 200
    meta = client.get("/api/meta").json()
    assert meta["setup_complete"] is False


def test_setup_validates_and_completes(admin):
    client = admin["client"]
    assert (
        client.post("/api/setup", json={"admin_password": "admin", "user_pin": "1357"}).status_code
        == 400
    )
    assert (
        client.post("/api/setup", json={"admin_password": "short", "user_pin": "1357"}).status_code
        == 400
    )
    assert (
        client.post(
            "/api/setup", json={"admin_password": "a good long password", "user_pin": "0000"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/setup", json={"admin_password": "a good long password", "user_pin": "12"}
        ).status_code
        == 400
    )

    good = client.post(
        "/api/setup", json={"admin_password": "a good long password", "user_pin": "13579"}
    )
    assert good.status_code == 200
    assert client.get("/api/meta").json()["setup_complete"] is True

    # old defaults are dead, new ones work
    assert client.post("/api/login", json={"role": "admin", "secret": "admin"}).status_code == 401
    assert client.post("/api/login", json={"role": "user", "secret": "0000"}).status_code == 401
    assert client.post("/api/login", json={"role": "user", "secret": "13579"}).status_code == 200
    assert client.get("/api/state").status_code == 200


def test_csrf_is_required_for_writes(ready):
    client = ready["client"]
    response = client.post(
        "/api/session/stop", json={"group_id": 1}, headers={"X-CSRF-Token": "nope"}
    )
    assert response.status_code == 403
    assert response.json()["code"] == "csrf"


def test_login_rate_limit_locks_the_client(settings):
    fake = FakeAdGuard()
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        complete_setup(client)
        for _ in range(4):
            assert (
                client.post("/api/login", json={"role": "user", "secret": "9999"}).status_code
                == 401
            )
        # the 5th failure locks the client IP for 15 minutes
        assert client.post("/api/login", json={"role": "user", "secret": "9999"}).status_code == 429
        response = client.post("/api/login", json={"role": "user", "secret": "2468"})
        assert response.status_code == 429
        assert response.json()["code"] == "locked"


# --------------------------------------------------------------------------- #
# groups
# --------------------------------------------------------------------------- #


def test_group_crud_and_reconcile(ready, fake):
    client = ready["client"]
    response = client.post(
        "/api/groups",
        json={"name": "YouTube", "emoji": "▶️", "weekday_minutes": 60, "services": ["youtube"]},
    )
    assert response.status_code == 200, response.text
    group = response.json()["group"]
    assert group["remaining_minutes"] == 60
    assert fake.ids == ["youtube"]  # blocked by default

    response = client.put(
        f"/api/groups/{group['id']}",
        json={
            "name": "YouTube",
            "weekday_minutes": 30,
            "weekend_minutes": 45,
            "services": ["youtube"],
        },
    )
    assert response.status_code == 200
    assert response.json()["group"]["weekend_minutes"] == 45

    # pause -> stays blocked, sessions refused
    client.post(f"/api/groups/{group['id']}/pause", json={"paused": True})
    assert (
        client.post("/api/session/start", json={"group_id": group["id"], "minutes": 15}).status_code
        == 409
    )

    assert client.delete(f"/api/groups/{group['id']}").status_code == 200
    assert fake.ids == []  # rules removed from AdGuard


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
    assert "||example-game.com^" in fake.rules

    second = client.post(
        "/api/groups",
        json={"name": "Other", "weekday_minutes": 45, "domains_text": "example-game.com"},
    )
    assert second.status_code == 400
    assert "already used" in second.json()["error"]


def test_critical_domain_needs_confirmation(ready, fake):
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
    assert "google-analytics.com" not in names  # noise
    assert "known.com" not in names  # already managed
    assert "youtube.com" in names  # not yet in a group here
    assert "roblox.com" not in names  # covered by a group's AdGuard service

    all_data = client.get("/api/discover?minutes=15&show_all=true").json()
    assert "google-analytics.com" in [d["domain"] for d in all_data["domains"]]


def test_test_endpoint_reports_blocked_hosts(ready, fake):
    client = ready["client"]
    group = client.post(
        "/api/groups",
        json={"name": "Games", "weekday_minutes": 45, "services": ["roblox"]},
    ).json()["group"]
    results = client.get(f"/api/groups/{group['id']}/test").json()["results"]
    hosts = {r["host"]: r["blocked"] for r in results}
    assert hosts["roblox.com"] is True


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #


def test_session_start_unblocks_then_blocks_at_expiry(ready, fake):
    client = ready["client"]
    group = client.post(
        "/api/groups",
        json={
            "name": "Games",
            "weekday_minutes": 2,
            "services": ["roblox"],
            "domains_text": "example-game-test.com",
        },
    ).json()["group"]
    assert fake.ids == ["roblox"]
    assert "||example-game-test.com^" in fake.rules

    started = client.post("/api/session/start", json={"group_id": group["id"], "minutes": 60})
    assert started.status_code == 200, started.text
    assert started.json()["session"]["planned_seconds"] == 120  # capped at the budget
    assert fake.ids == []
    assert fake.rules == []

    # force the session into the past and let the reconciler close it
    conn = client.app.state.conn
    conn.execute("UPDATE sessions SET started_at=started_at-180 WHERE ended_at IS NULL")
    conn.commit()
    client.app.state.reconciler.run("test")
    assert fake.ids == ["roblox"]
    assert "||example-game-test.com^" in fake.rules

    state = client.get("/api/state").json()
    entry = state["groups"][0]
    assert entry["remaining_minutes"] <= 0.5
    assert entry["active_session"] is None


def test_stop_bills_only_used_minutes(ready, fake):
    client = ready["client"]
    group = client.post(
        "/api/groups", json={"name": "Games", "weekday_minutes": 60, "services": ["roblox"]}
    ).json()["group"]
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
        group = client.post(
            "/api/groups", json={"name": "Games", "weekday_minutes": 60, "services": ["roblox"]}
        ).json()["group"]
        client.post("/api/session/start", json={"group_id": group["id"], "minutes": 15})
        assert fake.ids == []

    # "restart": a fresh app on the same data dir must close the session and block
    fake2 = FakeAdGuard()
    app2 = create_app(settings, adguard=fake2)
    with TestClient(app2) as client2:
        conn = client2.app.state.conn
        rows = list(conn.execute("SELECT end_reason, used_seconds FROM sessions"))
        assert rows and all(row["end_reason"] == "service_restart" for row in rows)
        assert fake2.ids == ["roblox"]


# --------------------------------------------------------------------------- #
# misc
# --------------------------------------------------------------------------- #


def test_healthz_and_meta_are_public(client):
    assert client.get("/healthz").json()["ok"] is True
    meta = client.get("/api/meta").json()
    assert meta["version"]
    assert "unofficial" in meta


def test_admin_only_endpoints_reject_user(ready):
    client = ready["client"]
    user = client.post("/api/login", json={"role": "user", "secret": "2468"})
    csrf = user.json()["csrf"]
    headers = {"X-CSRF-Token": csrf}
    assert client.get("/api/groups", headers=headers).status_code == 200
    assert (
        client.post(
            "/api/groups",
            json={"name": "x", "weekday_minutes": 1, "domains_text": "x.com"},
            headers=headers,
        ).status_code
        == 403
    )


def test_settings_tab_endpoints(ready, fake):
    client = ready["client"]
    assert client.post("/api/adguard/test", json={}).json()["ok"] is True
    assert (
        client.post("/api/admin/credentials", json={"role": "user", "secret": "1234"}).status_code
        == 200
    )
    assert (
        client.post("/api/admin/credentials", json={"role": "user", "secret": "0000"}).status_code
        == 400
    )
    assert (
        client.post("/api/admin/credentials", json={"role": "admin", "secret": "short"}).status_code
        == 400
    )
