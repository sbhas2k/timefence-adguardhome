"""Shared test fixtures: a fake AdGuard Home and a ready-to-use app."""

from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient

from timefence.api import create_app
from timefence.config import Settings

from .fakes import FakeAdGuard, make_entry

__all__ = ["FakeAdGuard", "make_entry", "locked_conn"]


def locked_conn(app):
    """Database access for tests: the app's store, used as a context manager."""
    return app.state.store


@pytest.fixture()
def settings(tmp_path):
    env = {
        "ADGUARD_URL": "http://127.0.0.1:3000",
        "ADGUARD_USER": "admin",
        "ADGUARD_PASSWORD": "secret",
        "BASE_PATH": "/",
        "TZ": "UTC",
        "DATA_DIR": str(tmp_path / "data"),
        "SECURE_COOKIES": "false",
        "RECONCILE_INTERVAL": "60",
        "LOG_LEVEL": "WARNING",
    }
    return Settings.from_env(env)


@pytest.fixture()
def fake():
    return FakeAdGuard()


@pytest.fixture(autouse=True)
def no_network(monkeypatch, request):
    """No test may reach the network; alert delivery is refused unless opted in."""
    # The app module imports the sender, so it is always loaded once the app
    # exists; the guard is inert only while the module itself is absent.
    alerts = sys.modules.get("timefence.alerts")
    attempts: list[tuple[object, object]] = []
    if alerts is None:
        yield attempts
        return
    if "deliveries" in request.fixturenames:
        yield attempts
        return

    def _refuse(target, message):
        attempts.append((target, message))
        raise AssertionError("a test tried to send an alert")

    monkeypatch.setattr(alerts, "_deliver", _refuse)
    yield attempts
    if attempts:
        pytest.fail("a test sent an alert without the deliveries fixture")


@pytest.fixture()
def deliveries():
    """Opt a delivery proof out of the network guard (bind a local stub server)."""
    from timefence import alerts

    return alerts._deliver


@pytest.fixture()
def app(settings, fake):
    return create_app(settings, adguard=fake)


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def admin(client):
    """Logged-in admin client plus its CSRF token."""
    response = client.post("/api/login", json={"username": "admin", "password": "admin"})
    assert response.status_code == 200, response.text
    csrf = response.json()["csrf"]
    client.headers.update({"X-CSRF-Token": csrf})
    return {"client": client, "csrf": csrf}


@pytest.fixture()
def ready(admin):
    """Admin that already completed first-run setup."""
    response = admin["client"].post(
        "/api/setup",
        json={"admin_password": "correct horse battery"},
    )
    assert response.status_code == 200, response.text
    # re-login because the admin password changed
    client = admin["client"]
    response = client.post(
        "/api/login", json={"username": "admin", "password": "correct horse battery"}
    )
    assert response.status_code == 200, response.text
    csrf = response.json()["csrf"]
    client.headers.update({"X-CSRF-Token": csrf})
    return {"client": client, "csrf": csrf}
