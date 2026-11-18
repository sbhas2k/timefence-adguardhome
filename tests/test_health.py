"""Health and shutdown behaviour of the app shell."""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from timefence.api import create_app

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
