"""Role boundaries: a user account is read-only and sees a reduced payload."""

from __future__ import annotations

import re

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from timefence.api import create_app

ADMIN = {"username": "admin", "password": "admin"}
ADMIN2 = {"username": "admin", "password": "correct horse battery"}

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
EXEMPT = {"/api/login", "/api/logout"}

APP_KEYS = {
    "name",
    "version",
    "role",
    "now",
    "timezone",
    "username",
    "csrf",
    "setup_complete",
}
GROUP_KEYS = {
    "id",
    "name",
    "emoji",
    "minutes_today",
    "bonus_minutes",
    "used_minutes",
    "remaining_minutes",
    "permitted",
    "status",
    "blocked_until",
    "free_until",
}


def sign_in(client, username: str, password: str) -> str:
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    csrf = response.json()["csrf"]
    client.headers.update({"X-CSRF-Token": csrf})
    return csrf


def complete_setup(client) -> None:
    sign_in(client, "admin", "admin")
    assert client.post("/api/setup", json={"admin_password": ADMIN2["password"]}).status_code == 200
    sign_in(client, "admin", ADMIN2["password"])


def create_kid(client, username: str = "kid", password: str = "kid-password") -> dict:
    created = client.post(
        "/api/users", json={"username": username, "password": password, "role": "user"}
    )
    assert created.status_code == 200, created.text
    return created.json()["user"]


def test_every_mutating_route_is_forbidden_for_users(settings, fake):
    client = TestClient(create_app(settings, adguard=fake))
    with client:
        complete_setup(client)
        create_kid(client)
        csrf = sign_in(client, "kid", "kid-password")

        routes = [route for route in client.app.routes if isinstance(route, APIRoute)]
        assert routes
        checked = 0
        for route in routes:
            if not (route.methods & MUTATING):
                continue
            path = re.sub(r"\{[^}]+\}", "1", route.path)
            if path in EXEMPT:
                continue
            for method in sorted(route.methods & MUTATING):
                response = client.request(method, path, json={}, headers={"X-CSRF-Token": csrf})
                assert response.status_code == 403, (method, path, response.text)
                assert response.json()["code"] == "read_only", (method, path, response.text)
                checked += 1
        assert checked >= 10


def test_user_can_log_out(settings, fake):
    client = TestClient(create_app(settings, adguard=fake))
    with client:
        complete_setup(client)
        create_kid(client)
        sign_in(client, "kid", "kid-password")
        assert client.get("/api/state").status_code == 200

        assert client.post("/api/logout", json={}).status_code == 200
        assert client.get("/api/state").status_code == 401


def test_user_state_is_reduced(settings, fake):
    client = TestClient(create_app(settings, adguard=fake))
    with client:
        complete_setup(client)
        create_kid(client)
        created = client.post(
            "/api/groups",
            json={"name": "Games", "weekday_minutes": 30, "services": ["roblox"]},
        )
        assert created.status_code == 200, created.text

        sign_in(client, "kid", "kid-password")
        state = client.get("/api/state").json()
        assert set(state) == {"app", "groups"}
        assert set(state["app"]) == APP_KEYS
        assert len(state["groups"]) == 1
        assert set(state["groups"][0]) == GROUP_KEYS

        listed = client.get("/api/groups").json()["groups"]
        assert set(listed[0]) == GROUP_KEYS


def test_user_sees_only_own_and_shared_groups(settings, fake):
    client = TestClient(create_app(settings, adguard=fake))
    with client:
        complete_setup(client)
        kid1 = create_kid(client, "kid1", "kid1-password")
        kid2 = create_kid(client, "kid2", "kid2-password")

        for name, owner, service in (
            ("Kid1 games", kid1["id"], "roblox"),
            ("Kid2 games", kid2["id"], "youtube"),
        ):
            created = client.post(
                "/api/groups",
                json={
                    "name": name,
                    "weekday_minutes": 20,
                    "services": [service],
                    "owner_id": owner,
                },
            )
            assert created.status_code == 200, created.text
        shared = client.post(
            "/api/groups", json={"name": "Shared", "weekday_minutes": 10, "services": ["instagram"]}
        )
        assert shared.status_code == 200, shared.text

        admin_names = {g["name"] for g in client.get("/api/state").json()["groups"]}
        assert admin_names == {"Kid1 games", "Kid2 games", "Shared"}

        sign_in(client, "kid1", "kid1-password")
        kid_names = {g["name"] for g in client.get("/api/state").json()["groups"]}
        assert kid_names == {"Kid1 games", "Shared"}
