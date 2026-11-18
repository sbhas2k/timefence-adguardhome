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


def _net(text: str):
    import ipaddress

    return ipaddress.ip_network(text)


def test_client_ip_uses_rightmost_untrusted_hop():
    trusted = (_net("127.0.0.1/32"),)
    assert security.client_ip("127.0.0.1", "6.6.6.6, 10.0.0.5", trusted) == "10.0.0.5"


def test_client_ip_cases():
    trusted = (_net("127.0.0.1/32"), _net("10.0.0.0/8"))
    assert security.client_ip("203.0.113.7", "6.6.6.6", trusted) == "203.0.113.7"
    assert security.client_ip("127.0.0.1", None, trusted) == "127.0.0.1"
    assert security.client_ip("127.0.0.1", "6.6.6.6", trusted) == "6.6.6.6"
    assert security.client_ip("127.0.0.1", "6.6.6.6,203.0.113.9", trusted) == "203.0.113.9"
    assert security.client_ip("127.0.0.1", "6.6.6.6, bogus", trusted) == "127.0.0.1"
    assert security.client_ip("127.0.0.1", "10.0.0.2, 10.0.0.3", trusted) == "10.0.0.2"
    assert security.client_ip("::ffff:127.0.0.1", None, (_net("127.0.0.1/32"),)) == "127.0.0.1"


def test_uvicorn_proxy_headers_disabled(tmp_path, monkeypatch):
    import uvicorn

    from timefence import __main__ as cli

    recorded: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: recorded.update(kwargs))
    monkeypatch.setenv("ADGUARD_PASSWORD", "secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    assert cli.main(["run", "--port", "8123"]) == 0
    assert recorded["proxy_headers"] is False
