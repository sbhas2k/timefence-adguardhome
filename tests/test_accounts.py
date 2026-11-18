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
