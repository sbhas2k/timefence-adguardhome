"""In-memory stand-ins for AdGuard Home, shared by the test suite."""

from __future__ import annotations

import http.server
import threading
import time
from datetime import UTC

from timefence.adguard import AdGuardClient, AdGuardError
from timefence.discover import parse_time


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

    def _maybe_fail(self) -> None:
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
        if self.fail or self.fail_all:
            return False, "connection refused"
        return True, "AdGuard Home v0.107.80-fake"


def make_entry(host: str, client_ip: str = "192.168.1.20", ago: int = 30) -> dict:
    ts = time.time() - ago
    from datetime import datetime

    stamp = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    return {"time": stamp, "client": client_ip, "question": {"name": host, "type": "A"}}


class StubServer:
    """A tiny local HTTP server for delivery proofs (never the real network)."""

    def __init__(self, status: int = 200, body: bytes = b"ok", delay: float = 0.0) -> None:
        self.status = status
        self.body = body
        self.delay = delay
        self.location = ""
        self.requests: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the base class names it
                length = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(length) if length else b""
                if outer.delay:
                    time.sleep(outer.delay)
                outer.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": payload}
                )
                self.send_response(outer.status)
                if 300 <= outer.status < 400 and outer.location:
                    self.send_header("Location", outer.location)
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
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
