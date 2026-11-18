"""Shared test fixtures: a fake AdGuard Home and a ready-to-use app."""

from __future__ import annotations

import contextlib

import pytest
from fastapi.testclient import TestClient

from timefence.api import create_app
from timefence.config import Settings

from .fakes import FakeAdGuard, make_entry

__all__ = ["FakeAdGuard", "make_entry", "locked_conn"]


def locked_conn(app):
    """Database access for tests: a context manager that hands out a connection."""
    return contextlib.nullcontext(app.state.conn)


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
