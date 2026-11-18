"""Account security: sessions end when the account changes."""

from __future__ import annotations

from fastapi.testclient import TestClient

from timefence.api import create_app

ADMIN = {"username": "admin", "password": "admin"}
ADMIN2 = {"username": "admin", "password": "correct horse battery"}


def sign_in(client, username: str, password: str) -> dict:
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    csrf = response.json()["csrf"]
    client.headers.update({"X-CSRF-Token": csrf})
    return response.json()


def complete_setup(client) -> None:
    sign_in(client, "admin", "admin")
    assert client.post("/api/setup", json={"admin_password": ADMIN2["password"]}).status_code == 200
    sign_in(client, "admin", ADMIN2["password"])


def create_user(client, username: str, password: str = "kid-password", role: str = "user") -> dict:
    created = client.post(
        "/api/users", json={"username": username, "password": password, "role": role}
    )
    assert created.status_code == 200, created.text
    return created.json()["user"]


def user_id(client, username: str) -> int:
    users = client.get("/api/users").json()["users"]
    return [row for row in users if row["username"] == username][0]["id"]


def test_disabled_user_session_is_rejected(settings, fake):
    app = create_app(settings, adguard=fake)
    with TestClient(app) as admin:
        complete_setup(admin)
        create_user(admin, "kid")

        kid = TestClient(app)
        sign_in(kid, "kid", "kid-password")
        assert kid.get("/api/state").status_code == 200

        assert (
            admin.post(f"/api/users/{user_id(admin, 'kid')}/disabled", json={"disabled": True})
        ).status_code == 200
        assert kid.get("/api/state").status_code == 401
        kid.close()


def test_deleted_user_session_is_rejected(settings, fake):
    app = create_app(settings, adguard=fake)
    with TestClient(app) as admin:
        complete_setup(admin)
        create_user(admin, "kid")
        kid = TestClient(app)
        sign_in(kid, "kid", "kid-password")
        assert kid.get("/api/state").status_code == 200

        assert admin.delete(f"/api/users/{user_id(admin, 'kid')}").status_code == 200
        assert kid.get("/api/state").status_code == 401
        kid.close()


def test_recreated_username_does_not_inherit_session(settings, fake):
    app = create_app(settings, adguard=fake)
    with TestClient(app) as admin:
        complete_setup(admin)
        create_user(admin, "kid")
        kid = TestClient(app)
        sign_in(kid, "kid", "kid-password")

        admin.delete(f"/api/users/{user_id(admin, 'kid')}")
        create_user(admin, "kid", "another-password")

        assert kid.get("/api/state").status_code == 401
        kid.close()


def test_demoted_admin_loses_admin_access_immediately(settings, fake):
    app = create_app(settings, adguard=fake)
    with TestClient(app) as admin:
        complete_setup(admin)
        create_user(admin, "second", "second-password", role="admin")

        second = TestClient(app)
        sign_in(second, "second", "second-password")
        assert second.get("/api/users").status_code == 200

        assert (
            admin.post(f"/api/users/{user_id(admin, 'second')}/role", json={"role": "user"})
        ).status_code == 200
        assert second.get("/api/users").status_code == 401
        assert second.get("/api/state").status_code == 401
        second.close()


def test_password_change_signs_out_other_sessions(settings, fake):
    app = create_app(settings, adguard=fake)
    with TestClient(app) as admin:
        complete_setup(admin)
        create_user(admin, "kid")
        kid = TestClient(app)
        sign_in(kid, "kid", "kid-password")
        assert kid.get("/api/state").status_code == 200

        assert (
            admin.post(
                f"/api/users/{user_id(admin, 'kid')}/password",
                json={"password": "brand new password"},
            )
        ).status_code == 200
        assert kid.get("/api/state").status_code == 401
        kid.close()


# --------------------------------------------------------------------------- #
# login hardening and lockout keys
# --------------------------------------------------------------------------- #


def test_login_requires_json(client):
    response = client.post("/api/login", data={"username": "admin", "password": "admin"})
    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_media_type"


def test_other_accounts_success_does_not_reset_lockout(settings, fake):
    app = create_app(settings, adguard=fake)
    with TestClient(app) as admin:
        complete_setup(admin)
        create_user(admin, "kid")
        kid = TestClient(app)
        sign_in(kid, "kid", "kid-password")

        for _ in range(4):
            assert (
                admin.post("/api/login", json={"username": "admin", "password": "nope"}).status_code
                == 401
            )
            sign_in(kid, "kid", "kid-password")  # a success for a different pair
        assert (
            admin.post("/api/login", json={"username": "admin", "password": "nope"}).status_code
            == 429
        )

        locked = admin.post(
            "/api/login", json={"username": "admin", "password": ADMIN2["password"]}
        )
        assert locked.status_code == 429
        kid.close()


def test_ip_lockout_after_20_failures_across_usernames(client):
    for index in range(10):
        for _ in range(2):
            response = client.post(
                "/api/login", json={"username": f"user{index}", "password": "nope"}
            )
            assert response.status_code == 401, response.text

    blocked = client.post("/api/login", json={"username": "admin", "password": "admin"})
    assert blocked.status_code == 429


def test_unknown_user_still_hashes(client, monkeypatch):
    from timefence import security

    seen: list[str] = []
    real = security.verify_secret

    def spy(stored: str, secret: str) -> bool:
        seen.append(stored)
        return real(stored, secret)

    monkeypatch.setattr(security, "verify_secret", spy)
    response = client.post("/api/login", json={"username": "nobody-here", "password": "whatever"})
    assert response.status_code == 401
    assert len(seen) == 1


def test_invalid_usernames_are_rate_limited(client, monkeypatch):
    from timefence import security

    calls = 0
    real = security.verify_secret

    def spy(stored: str, secret: str) -> bool:
        nonlocal calls
        calls += 1
        return real(stored, secret)

    monkeypatch.setattr(security, "verify_secret", spy)
    statuses = []
    for _ in range(20):
        response = client.post("/api/login", json={"username": "x" * 200, "password": "nope"})
        statuses.append(response.status_code)

    assert statuses[:19] == [401] * 19
    assert statuses[19] == 429
    assert calls == 20

    again = client.post("/api/login", json={"username": "x" * 200, "password": "nope"})
    assert again.status_code == 429
    assert calls == 20  # a locked address does not even run the dummy hash


def test_limiter_does_not_keep_invalid_usernames(settings, fake):
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        for _ in range(3):
            client.post("/api/login", json={"username": "x" * 200, "password": "nope"})
        keys = app.state.limiter.keys()
        assert not [key for key in keys if key.startswith("pair:")]
        assert [key for key in keys if key.startswith("ip:")]


def test_forwarded_for_header_lines_are_joined(settings, fake, monkeypatch):
    from timefence import security

    seen: list[str | None] = []
    real = security.client_ip

    def spy(peer, forwarded, trusted):
        seen.append(forwarded)
        return real(peer, forwarded, trusted)

    monkeypatch.setattr(security, "client_ip", spy)
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        client.post(
            "/api/login",
            json={"username": "admin", "password": "nope"},
            headers=[
                ("x-forwarded-for", "6.6.6.6"),
                ("x-forwarded-for", "203.0.113.9"),
            ],
        )
    assert seen == ["6.6.6.6,203.0.113.9"]
