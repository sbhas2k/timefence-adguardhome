"""In-memory stand-ins for AdGuard Home, shared by the test suite."""

from __future__ import annotations

import http.server
import json
import threading
import time
from collections.abc import Callable
from datetime import UTC
from pathlib import Path

from timefence.adguard import AdGuardAuthError, AdGuardClient, AdGuardError
from timefence.discover import parse_time

SERVICES_FIXTURE = Path(__file__).with_name("data") / "adguard_services.json"


class FakeAdGuard(AdGuardClient):
    """In-memory stand-in for the AdGuard Home control API."""

    SERVICES = [
        {
            "id": "youtube",
            "name": "YouTube",
            "rules": ["||youtube.com^", "||youtu.be^", "||googlevideo.com^", "||ytimg.com^"],
        },
        {"id": "instagram", "name": "Instagram", "rules": ["||instagram.com^"]},
        {"id": "roblox", "name": "Roblox", "rules": ["||roblox.com^"]},
    ]

    def __init__(self) -> None:  # noqa: D107 - no parent init on purpose
        self.ids: list[str] = []
        self.schedule: dict = {"time_zone": "Local"}
        self.rules: list[str] = []
        self.calls: list[tuple[str, object]] = []
        self.entries: list[dict] = []
        self.ql_config: dict = {
            "enabled": True,
            "interval": 7776000000,
            "anonymize_client_ip": False,
            "ignored": [],
        }
        self.fail = False
        self.fail_all = False
        self.delay = 0.0
        self.url = "fake://adguard"
        self.auth = ("admin", "admin")
        self.store = None

    def bind(self, store) -> None:
        """Remember the store so every call can prove the lock is not held."""
        self.store = store

    def _guard(self) -> None:
        if self.store is not None and self.store.lock.held_by_current_thread():
            raise AssertionError("AdGuard was called while the store lock was held")

    def _maybe_fail(self) -> None:
        self._guard()
        if self.fail_all:
            raise AdGuardError("fake failure")

    def _maybe_delay(self) -> None:
        if self.delay:
            time.sleep(self.delay)

    # -- blocked services ------------------------------------------------
    def blocked_services(self) -> dict:
        self._maybe_fail()
        return {"ids": list(self.ids), "schedule": dict(self.schedule)}

    def set_blocked_services(self, ids: list[str], schedule: dict) -> None:
        self._maybe_fail()
        self._maybe_delay()
        self.ids = sorted(set(ids))
        self.schedule = dict(schedule)
        self.calls.append(("services", list(self.ids)))

    def all_services(self) -> list[dict]:
        self._maybe_fail()
        return self.SERVICES

    # -- user rules ------------------------------------------------------
    def user_rules(self) -> list[str]:
        self._maybe_fail()
        return list(self.rules)

    def set_user_rules(self, rules: list[str]) -> None:
        self._maybe_fail()
        self._maybe_delay()
        self.rules = list(rules)
        self.calls.append(("rules", list(rules)))

    # -- queries ---------------------------------------------------------
    def check_host(self, name: str) -> dict:
        self._maybe_fail()
        blocked = False
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
        self._maybe_fail()
        return list(self.entries)

    def querylog_page(self, limit: int = 500, older_than: str | None = None) -> dict:
        """One page of the query log, newest first, as AdGuard returns it."""
        self._maybe_fail()
        self.calls.append(("querylog_page", older_than))
        cutoff = parse_time(older_than) if older_than else None
        entries = sorted(
            self.entries,
            key=lambda entry: parse_time(entry.get("time", "")) or 0,
            reverse=True,
        )
        if cutoff is not None:
            entries = [e for e in entries if (parse_time(e.get("time", "")) or 0) < cutoff]
        page = entries[:limit]
        return {"data": page, "oldest": page[-1]["time"] if page else ""}

    def querylog_config(self) -> dict:
        self._maybe_fail()
        return dict(self.ql_config)

    def status(self) -> dict:
        self._maybe_fail()
        return {"version": "v0.107.80-fake"}

    def test_connection(self) -> tuple[bool, str]:
        self._guard()
        if self.fail or self.fail_all:
            return False, "connection refused"
        return True, "AdGuard Home v0.107.80-fake"


def make_entry(host: str, client_ip: str = "192.168.1.20", ago: int = 30) -> dict:
    ts = time.time() - ago
    from datetime import datetime

    stamp = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    return {"time": stamp, "client": client_ip, "question": {"name": host, "type": "A"}}


def load_service_catalogue() -> list[dict]:
    """The recorded real catalogue: single-pipe, wildcard, modifier and regex rules."""
    return json.loads(SERVICES_FIXTURE.read_text(encoding="utf-8"))


def real_entry(
    host: str,
    stamp: str,
    client: str = "192.168.1.20",
    reason: str = "NotFilteredNotFound",
    answer: str = "",
    qtype: str = "A",
) -> dict:
    """One query-log entry shaped like the real server's (every field it sends)."""
    return {
        "time": stamp,
        "client": client,
        "reason": reason,
        "answer": answer,
        "status": "NOERROR",
        "cached": False,
        "client_proto": "udp",
        "elapsedMs": "1",
        "question": {"class": "INET", "name": host, "type": qtype},
    }


class FaithfulAdGuard(FakeAdGuard):
    """A stand-in shaped like a real AdGuard Home v0.107.80, quirks included.

    It models what the first run against the real server exposed and the older
    double never did: the per-address brute-force lock on failed sign-ins, query
    log pages that lose entries sharing a timestamp, `user_rules: null`,
    `ignored_enabled`, and a catalogue with the real rule shapes and icon sizes.
    Its clock is injectable, so the lock and its expiry can be driven in a test.
    """

    AUTH_ATTEMPTS = 5
    BLOCK_AUTH_SECONDS = 15 * 60

    def __init__(
        self,
        services: list[dict] | None = None,
        password: str = "secret",
        now: Callable[[], float] | None = None,
    ) -> None:
        super().__init__()
        self.SERVICES = load_service_catalogue() if services is None else services
        self.password = password
        self.expected_password = "secret"
        self._clock = now or time.monotonic
        self.auth_failures: list[float] = []
        self.blocked_until = 0.0
        self.auth_calls = 0
        self.reasons: dict[str, str] = {}
        self.rules = []
        self.ql_config = {
            "enabled": True,
            "interval": 7776000000,
            "ignored": [],
            # the real default: `ignored` entries are inert until this is on
            "ignored_enabled": False,
            "anonymize_client_ip": False,
        }

    # -- brute-force protection, as AdGuard Home applies it ---------------
    def _authenticate(self) -> None:
        self.auth_calls += 1
        now = self._clock()
        if now < self.blocked_until:
            raise AdGuardAuthError("GET /control: AdGuard Home rejected the credentials")
        # A failure counts while it is younger than the block window and a
        # successful sign-in clears the record, like the server's limiter.
        self.auth_failures = [
            failed for failed in self.auth_failures if now - failed < self.BLOCK_AUTH_SECONDS
        ]
        if self.password != self.expected_password:
            self.auth_failures.append(now)
            if len(self.auth_failures) >= self.AUTH_ATTEMPTS:
                self.blocked_until = now + self.BLOCK_AUTH_SECONDS
            raise AdGuardAuthError("GET /control: AdGuard Home rejected the credentials")
        self.auth_failures = []

    def _maybe_fail(self) -> None:
        self._authenticate()
        super()._maybe_fail()

    def test_connection(self) -> tuple[bool, str]:
        self._authenticate()
        return super().test_connection()

    # -- the API's own shapes ---------------------------------------------
    def user_rules(self) -> list[str] | None:
        self._maybe_fail()
        # the real server sends null, not [], when no rule exists
        return list(self.rules) if self.rules else None

    def check_host(self, name: str) -> dict:
        self._maybe_fail()
        if name in self.reasons:
            return {"reason": self.reasons[name]}
        for service in self.SERVICES:
            if service["id"] not in self.ids:
                continue
            for rule in service["rules"]:
                host = _rule_host(rule)
                if host and (name == host or name.endswith("." + host)):
                    return {"reason": "FilteredBlockedService", "service_name": service["name"]}
        for rule in self.rules:
            host = _rule_host(rule)
            if host and (name == host or name.endswith("." + host)):
                return {"reason": "FilteredBlackList"}
        return {"reason": "NotFilteredNotFound"}

    def querylog_page(self, limit: int = 500, older_than: str | None = None) -> dict:
        """Newest first, strict `older_than`, no cap, and a few out-of-order rows."""
        self._maybe_fail()
        self.calls.append(("querylog_page", older_than))
        cutoff = parse_time(older_than) if older_than else None
        entries = [
            entry
            for entry in self.entries
            if cutoff is None or (parse_time(entry.get("time", "")) or 0) < cutoff
        ]
        entries.sort(key=lambda entry: parse_time(entry.get("time", "")) or 0, reverse=True)
        for index in range(0, min(len(entries) - 1, 6), 2):
            entries[index], entries[index + 1] = entries[index + 1], entries[index]
        page = entries[:limit]
        # `oldest` is the last row of the page, which need not be the minimum
        return {"data": page, "oldest": page[-1]["time"] if page else ""}


def _rule_host(rule: str) -> str | None:
    """The host a rule names, parsed independently of the code under test."""
    text = (rule or "").strip()
    if not text or text.startswith(("!", "#", "/", "@@")):
        return None
    text = text.removeprefix("||").removeprefix("|")
    text = text.split("$", 1)[0].rstrip("^").strip(".").lower()
    if not text or any(char in text for char in "*|/, ~"):
        return None
    return text


class StubServer:
    """A tiny local HTTP server for delivery proofs (never the real network)."""

    def __init__(
        self,
        status: int = 200,
        body: bytes = b"ok",
        delay: float = 0.0,
        trickle: float = 0.0,
    ) -> None:
        self.status = status
        self.body = body
        self.delay = delay
        self.trickle = trickle
        self.location = ""
        self.requests: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the base class names it
                self._answer(b"")

            def do_POST(self) -> None:  # noqa: N802 - the base class names it
                length = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(length) if length else b""
                if outer.delay:
                    time.sleep(outer.delay)
                self._answer(payload)

            def _answer(self, payload: bytes) -> None:
                outer.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": payload}
                )
                self.send_response(outer.status)
                if 300 <= outer.status < 400 and outer.location:
                    self.send_header("Location", outer.location)
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                if outer.trickle:
                    for byte in outer.body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(outer.trickle)
                else:
                    self.wfile.write(outer.body)

            def log_message(self, *args) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> StubServer:
        self.thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"
