"""Session cookies, hashing and rate limiting."""

from __future__ import annotations

import threading
import time
import types

from timefence import security


def test_hashing_is_bounded(monkeypatch):
    """Concurrent logins must not start an unbounded number of key derivations."""
    active = 0
    peak = 0
    guard = threading.Lock()

    def verify(stored: str, secret: str) -> bool:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return True

    monkeypatch.setattr(
        security,
        "_ARGON2",
        types.SimpleNamespace(verify=verify, hash=lambda secret: "$argon2id$stub"),
    )

    results: list[bool] = []

    def work() -> None:
        results.append(security.verify_secret("$argon2id$stub", "x"))

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)

    assert results == [True] * 8
    assert peak <= 2, f"{peak} hashes ran at once"


def test_old_cookie_format_is_rejected():
    from timefence import security

    manager = security.SessionManager("a-secret")
    token = manager.serializer.dumps({"role": "admin", "csrf": "csrf-token", "username": "admin"})
    assert manager.read(token) is None
