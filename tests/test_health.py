"""Health and shutdown behaviour of the app shell."""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from timefence.api import create_app

from .conftest import FakeAdGuard

ADMIN = {"username": "admin", "password": "admin"}
ADMIN2 = {"username": "admin", "password": "correct horse battery"}


def sign_in_as_admin(client) -> None:
    login = client.post("/api/login", json=ADMIN)
    assert login.status_code == 200, login.text
    client.headers.update({"X-CSRF-Token": login.json()["csrf"]})
    assert client.post("/api/setup", json={"admin_password": ADMIN2["password"]}).status_code == 200
    login = client.post("/api/login", json=ADMIN2)
    assert login.status_code == 200, login.text
    client.headers.update({"X-CSRF-Token": login.json()["csrf"]})


def test_healthz_answers_while_adguard_is_slow(settings, fake):
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        sign_in_as_admin(client)
        created = client.post(
            "/api/groups",
            json={"name": "YouTube", "weekday_minutes": 60, "services": ["youtube"]},
        )
        assert created.status_code == 200, created.text
        group_id = created.json()["group"]["id"]

        fake.delay = 3.0
        failures: list[BaseException] = []

        def slow_action() -> None:
            try:
                # the pause handler is awaited on the event loop in the old design
                client.post(f"/api/groups/{group_id}/pause", json={"paused": True})
            except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
                failures.append(exc)

        thread = threading.Thread(target=slow_action, name="slow-admin-action")
        thread.start()
        time.sleep(0.3)
        started = time.monotonic()
        response = client.get("/healthz")
        elapsed = time.monotonic() - started
        thread.join(30)
        fake.delay = 0.0

        assert not failures
        assert response.status_code == 200
        assert elapsed < 1.5, f"/healthz took {elapsed:.2f}s while AdGuard was slow"


def test_shutdown_blocks_every_group(settings, fake):
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        sign_in_as_admin(client)
        created = client.post(
            "/api/groups",
            json={
                "name": "YouTube",
                "weekday_minutes": 60,
                "services": ["youtube"],
                "domains_text": "example.com",
            },
        )
        assert created.status_code == 200, created.text
        assert fake.ids == []  # budget left: allowed

    assert fake.ids == ["youtube"]
    assert "||example.com^" in fake.rules


def test_shutdown_block_wins_over_a_running_loop(settings, fake):
    app = create_app(settings, adguard=fake)
    reconciler = app.state.reconciler
    with TestClient(app) as client:
        sign_in_as_admin(client)
        created = client.post(
            "/api/groups",
            json={"name": "YouTube", "weekday_minutes": 60, "services": ["youtube"]},
        )
        assert created.status_code == 200, created.text
        fake.ids = ["youtube"]  # the next run has an unblock write pending
        fake.delay = 1.0
        thread = threading.Thread(target=reconciler.run, args=("loop",), name="slow-run")
        thread.start()
        time.sleep(0.1)

    thread.join(30)
    fake.delay = 0.0
    assert not thread.is_alive()
    assert "youtube" in fake.ids
    services_calls = [entry for entry in fake.calls if entry[0] == "services"]
    assert services_calls and "youtube" in services_calls[-1][1]


def test_healthz_degraded_when_sync_is_stale(settings, fake):
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200

        app.state.reconciler.last_ok_ts = time.time() - 3 * settings.reconcile_interval - 1
        response = client.get("/healthz")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"

        app.state.reconciler.last_ok_ts = time.time()
        assert client.get("/healthz").status_code == 200


def test_healthz_degraded_when_adguard_fails(settings, fake):
    fake.fail_all = True
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"


def test_admin_state_reports_sync_and_warnings(settings, fake):
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        sign_in_as_admin(client)
        state = client.get("/api/state").json()
        assert isinstance(state["last_sync"], int)
        assert state["warnings"] == ["tz_utc"]  # the fixture runs with TZ=UTC

        app.state.reconciler.warnings.add("rules_section_damaged")
        state = client.get("/api/state").json()
        assert state["warnings"] == ["rules_section_damaged", "tz_utc"]


def test_restart_restores_true_state_after_shutdown_block(settings, fake):
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        sign_in_as_admin(client)
        created = client.post(
            "/api/groups",
            json={"name": "YouTube", "weekday_minutes": 60, "services": ["youtube"]},
        )
        assert created.status_code == 200, created.text
        assert fake.ids == []
    assert fake.ids == ["youtube"]  # a clean stop blocks every managed group

    fake2 = FakeAdGuard()
    app2 = create_app(settings, adguard=fake2)
    with TestClient(app2):
        assert fake2.ids == []  # budget left: allowed again


def test_create_app_sets_private_umask(settings, fake, monkeypatch):
    import os

    calls: list[int] = []

    def spy(mask: int) -> int:
        calls.append(mask)
        return 0o022

    monkeypatch.setattr(os, "umask", spy)
    app = create_app(settings, adguard=fake)
    try:
        assert 0o077 in calls
    finally:
        app.state.store.close()


def test_tz_utc_warning_in_admin_state(settings, fake):
    assert settings.tz == "UTC"
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        sign_in_as_admin(client)
        warnings = client.get("/api/state").json()["warnings"]
        assert "tz_utc" in warnings
