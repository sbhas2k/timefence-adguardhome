"""Thin client for the AdGuard Home control API.

Only the handful of endpoints TimeFence needs.  Everything is read-modify-write
so that settings owned by somebody else are never clobbered.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

from .discover import parse_time

log = logging.getLogger("timefence.adguard")

BLOCKED_REASONS = {
    "FilteredBlackList",
    "FilteredBlockedService",
    "FilteredSafeBrowsing",
    "FilteredParental",
    "FilteredSafeSearch",
}


class AdGuardError(RuntimeError):
    """Any failure talking to AdGuard Home."""


class AdGuardClient:
    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        timeout: float = 10.0,
        session: requests.Session | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.auth = (user, password)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    # ------------------------------------------------------------------ #
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self.timeout)
        try:
            resp = self.session.request(method, f"{self.url}{path}", auth=self.auth, **kwargs)
        except requests.RequestException as exc:
            raise AdGuardError(f"{method} {path}: {exc}") from exc
        if resp.status_code >= 400:
            raise AdGuardError(f"{method} {path}: HTTP {resp.status_code} {resp.text[:200]}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def _request_json(self, method: str, path: str, *, expect: type = dict, **kwargs: Any) -> Any:
        """Like `_request`, but refuse a body of the wrong shape.

        AdGuard answers HTML (a captive portal, a stopped service, an error
        page) with status 200; callers must see an AdGuardError, never a str.
        """
        data = self._request(method, path, **kwargs)
        if data is None:
            return None
        if not isinstance(data, expect):
            raise AdGuardError(f"{method} {path}: unexpected response")
        return data

    # -- service status --------------------------------------------------
    def status(self) -> dict:
        return self._request_json("GET", "/control/status") or {}

    def test_connection(self) -> tuple[bool, str]:
        try:
            data = self.status()
        except AdGuardError as exc:
            return False, str(exc)
        version = data.get("version", "?")
        return True, f"AdGuard Home {version}"

    # -- blocked services ------------------------------------------------
    def blocked_services(self) -> dict:
        data = self._request_json("GET", "/control/blocked_services/get") or {}
        return {
            "ids": list(data.get("ids") or []),
            "schedule": data.get("schedule") or {"time_zone": "Local"},
        }

    def set_blocked_services(self, ids: list[str], schedule: dict) -> None:
        self._request(
            "PUT",
            "/control/blocked_services/update",
            data=json.dumps({"ids": sorted(set(ids)), "schedule": schedule}),
        )

    def all_services(self) -> list[dict]:
        data = self._request_json("GET", "/control/blocked_services/all") or {}
        return list(data.get("blocked_services") or [])

    # -- user rules ------------------------------------------------------
    def user_rules(self) -> list[str]:
        data = self._request_json("GET", "/control/filtering/status") or {}
        return list(data.get("user_rules") or [])

    def set_user_rules(self, rules: list[str]) -> None:
        self._request("POST", "/control/filtering/set_rules", data=json.dumps({"rules": rules}))

    # -- checking / discovery -------------------------------------------
    def check_host(self, name: str) -> dict:
        data = self._request_json("GET", "/control/filtering/check_host", params={"name": name})
        return data if isinstance(data, dict) else {}

    def is_blocked(self, name: str) -> bool:
        return self.check_host(name).get("reason") in BLOCKED_REASONS

    # -- query log -------------------------------------------------------
    def querylog_page(self, limit: int = 500, older_than: str | None = None) -> dict:
        """One page, newest first; `older_than` is an AdGuard timestamp string."""
        params: dict[str, Any] = {"limit": limit}
        if older_than:
            params["older_than"] = older_than
        return self._request_json("GET", "/control/querylog", params=params) or {}

    def querylog_since(
        self, since_ts: int, page_size: int = 500, max_pages: int = 20
    ) -> tuple[list[dict], bool]:
        """Every entry newer than `since_ts`, plus whether the log ran out of pages."""
        entries: list[dict] = []
        older_than: str | None = None
        for _ in range(max_pages):
            page = self.querylog_page(limit=page_size, older_than=older_than)
            data = list(page.get("data") or [])
            if not data:
                return entries, False
            entries.extend(data)
            oldest = page.get("oldest")
            oldest_ts = parse_time(oldest) if isinstance(oldest, str) else None
            if oldest_ts is None or oldest_ts <= since_ts or oldest == older_than:
                return entries, False
            older_than = oldest
        return entries, True

    def querylog_config(self) -> dict | None:
        """The query-log settings, or None when AdGuard does not offer them."""
        try:
            data = self._request_json("GET", "/control/querylog/config")
        except AdGuardError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        return data if isinstance(data, dict) else None
