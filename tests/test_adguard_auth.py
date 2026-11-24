"""Rejected AdGuard credentials: one attempt per window, then recovery.

AdGuard Home counts failed authentications per address and stops answering for
15 minutes, even to the right password. A cycle that keeps calling therefore
locks TimeFence (and everybody else on that address) out.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from timefence import alerts, budget, db
from timefence.adguard import AdGuardAuthError
from timefence.api import create_app
from timefence.reconciler import AUTH_BACKOFF_BASE, AUTH_BACKOFF_MAX, Reconciler

from .fakes import FaithfulAdGuard

TZ = ZoneInfo("UTC")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def store(tmp_path):
    store = db.Store(tmp_path / "auth.db")
    with store as conn:
        db.migrate(conn, TZ)
    yield store
    store.close()


def build(store, password="wrong"):
    clock = Clock()
    fake = FaithfulAdGuard(password=password, now=clock)
    reconciler = Reconciler(store, fake, TZ)
    reconciler.monotonic = clock
    return reconciler, fake, clock


def test_one_call_per_cycle_and_a_doubling_backoff(store):
    reconciler, fake, clock = build(store)

    reconciler.run("loop", now=int(clock.now))
    assert fake.auth_calls == 1
    assert reconciler.last_error_kind == "auth"
    assert reconciler.backoff_remaining() == int(AUTH_BACKOFF_BASE)
    assert "credentials" in (reconciler.last_error or "")

    clock.now = AUTH_BACKOFF_BASE - 1
    reconciler.run("loop", now=int(clock.now))
    assert fake.auth_calls == 1  # still inside the window: no call at all

    clock.now = AUTH_BACKOFF_BASE
    reconciler.run("loop", now=int(clock.now))
    assert fake.auth_calls == 2
    assert reconciler.backoff_remaining() == 2 * AUTH_BACKOFF_BASE

    clock.now += 2 * AUTH_BACKOFF_BASE
    reconciler.run("loop", now=int(clock.now))
    assert fake.auth_calls == 3
    assert reconciler.backoff_remaining() == 4 * AUTH_BACKOFF_BASE


def test_ten_cycles_never_reach_the_lock(store):
    reconciler, fake, clock = build(store)
    for _ in range(10):
        reconciler.run("loop", now=int(clock.now))
        clock.now += reconciler.backoff_remaining() or int(AUTH_BACKOFF_MAX)
    assert fake.auth_calls == 10
    assert fake.blocked_until == 0.0
    assert reconciler.backoff_remaining() <= int(AUTH_BACKOFF_MAX)


def test_a_fixed_password_recovers_after_the_window(store):
    reconciler, fake, clock = build(store)
    reconciler.run("loop", now=int(clock.now))
    assert reconciler.last_ok_ts is None

    fake.password = "secret"
    clock.now += reconciler.backoff_remaining()
    result = reconciler.run("loop", now=int(clock.now))

    assert result["errors"] == []
    assert reconciler.last_error_kind is None
    assert reconciler.backoff_remaining() == 0
    assert reconciler.last_ok_ts is not None
    assert fake.blocked_until == 0.0


def test_an_admin_connection_test_clears_the_backoff(store):
    reconciler, fake, clock = build(store)
    reconciler.run("loop", now=int(clock.now))
    assert reconciler.backoff_remaining() > 0

    fake.password = "secret"
    ok, _ = reconciler.test_connection()
    assert ok
    assert reconciler.backoff_remaining() == 0
    reconciler.run("loop", now=int(clock.now))
    assert reconciler.last_error_kind is None


def test_shutdown_and_release_stop_at_the_first_rejection(store):
    reconciler, fake, clock = build(store)
    result = reconciler.block_all()
    assert fake.auth_calls == 1
    assert result["changed"] == []
    assert result["errors"]

    def make() -> Reconciler:
        other, _, _ = build(store)
        return other

    fresh = make()
    with pytest.raises(AdGuardAuthError):
        fresh.release()
    assert fresh.adguard.auth_calls == 1


def test_auth_failure_keeps_healthz_degraded(settings):
    clock = Clock()
    fake = FaithfulAdGuard(password="wrong", now=clock)
    app = create_app(settings, adguard=fake)
    app.state.reconciler.monotonic = clock
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 503
        app.state.reconciler.run("loop")
        assert client.get("/healthz").status_code == 503


def test_the_admin_banner_names_the_rejected_credentials(settings):
    clock = Clock()
    fake = FaithfulAdGuard(password="wrong", now=clock)
    app = create_app(settings, adguard=fake)
    app.state.reconciler.monotonic = clock
    with TestClient(app) as client:
        login = client.post("/api/login", json={"username": "admin", "password": "admin"})
        assert login.status_code == 200, login.text
        client.headers.update({"X-CSRF-Token": login.json()["csrf"]})
        setup = client.post("/api/setup", json={"admin_password": "correct horse battery"})
        assert setup.status_code == 200, setup.text
        app.state.reconciler.run("loop")
        state = client.get("/api/state").json()

    assert state["adguard_error_kind"] == "auth"
    assert 1 <= state["adguard_retry_seconds"] <= int(AUTH_BACKOFF_MAX)
    assert "credentials" in state["adguard_error"]


def test_alerts_never_carry_the_password(store, settings):
    reconciler, fake, clock = build(store)
    with store as conn:
        alerts.write_config(conn, {"channel": "ntfy", "ntfy_topic": "kid-alerts"})
        budget.create_group(conn, name="YouTube", emoji="", services=["youtube"])
    worker = alerts.AlertWorker(store, settings)
    reconciler.alert_worker = worker
    # AdGuard rejects the credentials on every run; the probe still reports it
    for step in range(4):
        reconciler.run("loop", now=step)
    queued = []
    while not worker.queue.empty():
        message = worker.queue.get_nowait()
        if message is not None:
            queued.append(message)
    assert [message.event for message in queued] == ["sync_failing"]
    text = alerts.message_text(queued[0])
    payload = str(alerts.webhook_payload(queued[0]))
    for body in (text, payload):
        assert "secret" not in body
        assert "wrong" not in body


def test_a_rejection_skips_the_remaining_phases(store):
    class LateRejection(FaithfulAdGuard):
        def __init__(self) -> None:
            super().__init__()
            self.denied = 0
            self.user_rules_calls = 0

        def blocked_services(self):
            self.auth_calls += 1
            self.denied += 1
            raise AdGuardAuthError("AdGuard Home rejected the credentials")

        def user_rules(self):
            self.user_rules_calls += 1
            return super().user_rules()

    fake = LateRejection()
    reconciler = Reconciler(store, fake, TZ)
    result = reconciler.run("loop", now=1_700_000_000)

    assert fake.denied == 1  # one rejected call, not one per phase
    assert fake.user_rules_calls == 0  # the domain phase never ran
    assert result["errors"]
    assert reconciler.last_error_kind == "auth"
