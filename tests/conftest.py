"""Shared test fixtures: a fake AdGuard Home and a ready-to-use app."""

from __future__ import annotations

import time
from datetime import UTC

import pytest
from fastapi.testclient import TestClient

from timefence.adguard import AdGuardClient
from timefence.api import create_app
from timefence.config import Settings


class FakeAdGuard(AdGuardClient):
    """In-memory stand-in for the AdGuard Home control API."""

    SERVICES = [
        {"id": "youtube", "name": "YouTube", "rules": ["||youtube.com^", "||youtu.be^"]},
        {"id": "instagram", "name": "Instagram", "rules": ["||instagram.com^"]},
        {"id": "roblox", "name": "Roblox", "rules": ["||roblox.com^"]},
    ]

    def __init__(self) -> None:  # noqa: D107 - no parent init on purpose
        self.ids: list[str] = []
        self.schedule: dict = {"time_zone": "Local"}
        self.rules: list[str] = []
        self.calls: list[tuple[str, object]] = []
        self.entries: list[dict] = []
        self.fail = False
        self.url = "fake://adguard"
        self.auth = ("admin", "admin")

    # -- blocked services ------------------------------------------------
    def blocked_services(self) -> dict:
        return {"ids": list(self.ids), "schedule": dict(self.schedule)}

    def set_blocked_services(self, ids: list[str], schedule: dict) -> None:
        self.ids = sorted(set(ids))
        self.schedule = dict(schedule)
        self.calls.append(("services", list(self.ids)))

    def all_services(self) -> list[dict]:
        return self.SERVICES

    # -- user rules ------------------------------------------------------
    def user_rules(self) -> list[str]:
        return list(self.rules)

    def set_user_rules(self, rules: list[str]) -> None:
        self.rules = list(rules)
        self.calls.append(("rules", list(rules)))

    # -- queries ---------------------------------------------------------
    def check_host(self, name: str) -> dict:
        blocked = False
        if any(part in self.ids for part in self.ids):
            for service in self.SERVICES:
                if service["id"] not in self.ids:
                    continue
                for rule in service["rules"]:
                    host = rule.removeprefix("||").removesuffix("^")
                    if name == host or name.endswith("." + host):
                        blocked = True
        for rule in self.rules:
            if not rule.startswith("||"):
                continue
            host = rule.removeprefix("||").removesuffix("^")
            if name == host or name.endswith("." + host):
                blocked = True
        return {"reason": "FilteredBlackList" if blocked else "NotFilteredNotFound"}

    def querylog(self, limit: int = 2000, search: str = "") -> list[dict]:
        return list(self.entries)

    def status(self) -> dict:
        return {"version": "v0.107.80-fake"}

    def test_connection(self) -> tuple[bool, str]:
        if self.fail:
            return False, "connection refused"
        return True, "AdGuard Home v0.107.80-fake"


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
    response = client.post("/api/login", json={"role": "admin", "secret": "admin"})
    assert response.status_code == 200, response.text
    csrf = response.json()["csrf"]
    client.headers.update({"X-CSRF-Token": csrf})
    return {"client": client, "csrf": csrf}


@pytest.fixture()
def ready(admin):
    """Admin that already completed first-run setup."""
    response = admin["client"].post(
        "/api/setup",
        json={"admin_password": "correct horse battery", "user_pin": "2468"},
    )
    assert response.status_code == 200, response.text
    # re-login because the admin password changed
    client = admin["client"]
    response = client.post("/api/login", json={"role": "admin", "secret": "correct horse battery"})
    assert response.status_code == 200, response.text
    csrf = response.json()["csrf"]
    client.headers.update({"X-CSRF-Token": csrf})
    return {"client": client, "csrf": csrf}


def make_entry(host: str, client_ip: str = "192.168.1.20", ago: int = 30) -> dict:
    ts = time.time() - ago
    from datetime import datetime

    stamp = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    return {"time": stamp, "client": client_ip, "question": {"name": host, "type": "A"}}
