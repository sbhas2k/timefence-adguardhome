"""Keep AdGuard Home in sync with the intended state.

Everything here is diff-based: AdGuard is only written to when the desired state
differs from what AdGuard currently reports, so foreign rules/services (and any
change a human makes in the AdGuard UI) are preserved.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from zoneinfo import ZoneInfo

from . import budget
from .adguard import AdGuardClient, AdGuardError

log = logging.getLogger("timefence.reconciler")

SECTION_BEGIN = "# timefence:begin"
SECTION_END = "# timefence:end"
LEGACY_MARKERS = ("budget-app",)  # migrated silently on the first run


def strip_section(
    rules: list[str], begin: str = SECTION_BEGIN, end: str = SECTION_END
) -> list[str]:
    """Return the rules with the managed section (and its markers) removed."""
    out: list[str] = []
    inside = False
    for line in rules:
        stripped = line.strip()
        if stripped == begin:
            inside = True
            continue
        if stripped == end:
            inside = False
            continue
        if not inside:
            out.append(line)
    return out


def strip_legacy_sections(rules: list[str]) -> list[str]:
    for marker in LEGACY_MARKERS:
        rules = strip_section(rules, f"# {marker}:begin", f"# {marker}:end")
    return rules


def build_rules(current: list[str], desired_domains: list[str]) -> list[str]:
    """Replace our section in `current`, preserving every foreign line verbatim."""
    base = strip_legacy_sections(strip_section(current))
    if not desired_domains:
        return base
    return base + [SECTION_BEGIN] + [f"||{d}^" for d in desired_domains] + [SECTION_END]


def desired_services(conn: sqlite3.Connection, tz: ZoneInfo, now: int | None = None) -> set[str]:
    now = now or int(time.time())
    wanted: set[str] = set()
    for group in budget.list_groups(conn):
        if budget.is_permitted(conn, group, tz, now):
            continue
        wanted.update(group.services)
    return wanted


def desired_domains(conn: sqlite3.Connection, tz: ZoneInfo, now: int | None = None) -> list[str]:
    now = now or int(time.time())
    out: list[str] = []
    for group in budget.list_groups(conn):
        if budget.is_permitted(conn, group, tz, now):
            continue
        for domain in group.domains:
            if domain not in out:
                out.append(domain)
    return sorted(out)


class Reconciler:
    def __init__(self, conn: sqlite3.Connection, adguard: AdGuardClient, tz: ZoneInfo) -> None:
        self.conn = conn
        self.adguard = adguard
        self.tz = tz
        self.last_error: str | None = None
        self.last_result: dict = {}

    # -- individual halves, both idempotent ------------------------------
    def sync_services(self, now: int | None = None) -> list[str]:
        now = now or int(time.time())
        # Only services TimeFence itself took over are ever removed from the
        # AdGuard list; everything else the operator configured stays as-is.
        owned = budget.owned_services(self.conn)
        current = self.adguard.blocked_services()
        current_ids = list(current["ids"])
        wanted = {i for i in current_ids if i not in owned} | desired_services(
            self.conn, self.tz, now
        )
        if set(current_ids) == wanted:
            return []
        self.adguard.set_blocked_services(sorted(wanted), current["schedule"])
        log.info("blocked services: %s -> %s", sorted(current_ids), sorted(wanted))
        return sorted(wanted ^ set(current_ids))

    def sync_domains(self, now: int | None = None) -> bool:
        now = now or int(time.time())
        current = self.adguard.user_rules()
        desired = desired_domains(self.conn, self.tz, now)
        updated = build_rules(current, desired)
        if updated == current:
            return False
        self.adguard.set_user_rules(updated)
        log.info("user rules updated: %d managed domain(s) blocked", len(desired))
        return True

    # -- full pass -------------------------------------------------------
    def run(self, reason: str = "loop", now: int | None = None) -> dict:
        now = now or int(time.time())
        result: dict = {"reason": reason, "changed": [], "errors": []}
        closed = budget.close_expired(self.conn, now)
        if closed:
            result["closed"] = len(closed)
            for row in closed:
                budget.log_event(
                    self.conn,
                    "session_end",
                    {
                        "group_id": row["group_id"],
                        "reason": "expired",
                        "used_seconds": row["used_seconds"],
                    },
                )
        try:
            changed_services = self.sync_services(now)
            if changed_services:
                result["changed"].append("services")
        except AdGuardError as exc:
            log.error("service sync failed: %s", exc)
            result["errors"].append(str(exc))
        try:
            if self.sync_domains(now):
                result["changed"].append("domains")
        except AdGuardError as exc:
            log.error("domain sync failed: %s", exc)
            result["errors"].append(str(exc))
        self.last_error = result["errors"][0] if result["errors"] else None
        self.last_result = result
        return result
