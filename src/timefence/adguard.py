"""Thin client for the AdGuard Home control API.

Only the handful of endpoints TimeFence needs.  Everything is read-modify-write
so that settings owned by somebody else are never clobbered.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

import requests

from .discover import parse_time

log = logging.getLogger("timefence.adguard")


class AdGuardError(RuntimeError):
    """Any failure talking to AdGuard Home."""


class AdGuardAuthError(AdGuardError):
    """AdGuard Home rejected the credentials (HTTP 401 or 403).

    Its own type because repeating the call makes things worse: AdGuard Home
    counts failed authentications per address and stops answering for minutes.
    """


AUTH_STATUSES = frozenset({401, 403})

TIMESTAMP = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<frac>\d+))?(?P<tz>Z|[+-]\d{2}:\d{2})?$"
)


def next_timestamp(text: str) -> str | None:
    """`text` moved forward by at least a microsecond, in its own precision.

    AdGuard's `older_than` is strict, and its log can hold several entries with
    one timestamp (a burst from one client, or a coarse clock). Paging with the
    page's own last stamp would silently skip the rest of that group, so the
    cursor steps past it; repeats are de-duplicated by entry key. The step is at
    least 1 us, because that is the finest difference this side can represent.
    """
    match = TIMESTAMP.match((text or "").strip())
    if not match:
        return None
    base, frac, tz = match.group("base"), match.group("frac"), match.group("tz") or ""
    digits = len(frac or "")
    if digits:
        unit_ns = 10 ** (9 - digits)
        step = max(1, -(-1000 // unit_ns))  # nearest whole unit to 1 us
        value = int(frac) + step
        if value < 10**digits:
            return f"{base}.{value:0{digits}d}{tz}"
    try:
        after = datetime.fromisoformat(base + tz) + timedelta(seconds=1)
    except ValueError:
        return None
    stamp = after.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{stamp}.{'0' * digits}{tz}" if digits else f"{stamp}{tz}"


def entry_key(entry: dict) -> tuple[str, ...]:
    """A stable identity for one query-log row (timestamps are not unique)."""
    question = entry.get("question") or {}
    return (
        str(entry.get("time") or ""),
        str(entry.get("client") or ""),
        str(question.get("name") or ""),
        str(question.get("type") or ""),
        str(entry.get("reason") or ""),
        str(entry.get("answer") or ""),
    )


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
        except requests.exceptions.Timeout:
            # never the urllib3 text, the URL or the credentials: this string is
            # shown in the admin banner and kept in the state
            raise AdGuardError(f"AdGuard Home did not answer within {self.timeout:g} s") from None
        except requests.RequestException:
            raise AdGuardError("cannot reach AdGuard Home") from None
        if resp.status_code in AUTH_STATUSES:
            raise AdGuardAuthError("AdGuard Home rejected the credentials")
        if resp.status_code >= 400:
            raise AdGuardError(f"AdGuard Home answered HTTP {resp.status_code}")
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
            raise AdGuardError("AdGuard Home answered an unexpected response")
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

    # -- query log -------------------------------------------------------
    def querylog_page(self, limit: int = 500, older_than: str | None = None) -> dict:
        """One page, newest first; `older_than` is an AdGuard timestamp string."""
        params: dict[str, Any] = {"limit": limit}
        if older_than:
            params["older_than"] = older_than
        return self._request_json("GET", "/control/querylog", params=params) or {}

    def querylog_since(
        self, since_ts: int, page_size: int = 5000, max_pages: int = 20
    ) -> tuple[list[dict], bool]:
        """Every entry newer than `since_ts`, plus whether the log ran out of pages.

        Pages are read by timestamp, not by offset, so a busy log can shift
        under us; rows are de-duplicated by key and the cursor skips past the
        page's oldest stamp so a tie at the boundary is not lost.
        """
        entries: list[dict] = []
        seen: set[tuple[str, ...]] = set()
        older_than: str | None = None
        for _ in range(max_pages):
            page = self.querylog_page(limit=page_size, older_than=older_than)
            data = list(page.get("data") or [])
            if not data:
                return entries, False
            added = 0
            for entry in data:
                key = entry_key(entry)
                if key in seen:
                    continue
                seen.add(key)
                entries.append(entry)
                added += 1
            stamps = [
                (parse_time(entry.get("time", "")), entry.get("time", ""))
                for entry in data
                if isinstance(entry.get("time"), str)
            ]
            stamps = [pair for pair in stamps if pair[0] is not None]
            if not stamps:
                return entries, False
            oldest_ts, oldest_text = min(stamps, key=lambda pair: pair[0])
            if oldest_ts <= since_ts:
                return entries, False
            cursor = next_timestamp(oldest_text)
            if cursor is None or cursor == older_than or added == 0:
                return entries, False
            older_than = cursor
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
