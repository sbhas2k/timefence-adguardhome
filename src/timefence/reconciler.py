"""Keep AdGuard Home in sync with the intended state.

Everything here is diff-based: AdGuard is only written to when the desired state
differs from what AdGuard currently reports, so foreign rules/services (and any
change a human makes in the AdGuard UI) are preserved.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo

from . import budget, db
from .adguard import AdGuardClient, AdGuardError
from .discover import parse_time

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
    def __init__(self, store: db.Store, adguard: AdGuardClient, tz: ZoneInfo) -> None:
        self.store = store
        self.adguard = adguard
        self.tz = tz
        self.last_error: str | None = None
        self.last_result: dict = {}
        self._run_lock = threading.Lock()
        self._service_domains: dict[str, set[str]] = {}
        self._service_domains_ts = 0.0

    # -- usage metering (auto mode) --------------------------------------
    def service_domains(self) -> dict[str, set[str]]:
        """service id -> the hostnames its AdGuard rules cover (cached)."""
        now = time.time()
        if now - self._service_domains_ts > 300 or not self._service_domains:
            mapping: dict[str, set[str]] = {}
            for service in self.adguard.all_services():
                hosts: set[str] = set()
                for rule in service.get("rules") or []:
                    if not rule.startswith("||"):
                        continue
                    host = rule[2:].removesuffix("^").lstrip(".").lower()
                    if host and "*" not in host:
                        hosts.add(host)
                mapping[service["id"]] = hosts
            self._service_domains = mapping
            self._service_domains_ts = now
        return self._service_domains

    def matcher(self, group: budget.Group) -> set[str]:
        suffixes = {d.lower() for d in group.domains}
        if group.services:
            domains = self.service_domains()
            for service_id in group.services:
                suffixes |= domains.get(service_id, set())
        return suffixes

    @staticmethod
    def host_matches(host: str, suffixes: set[str]) -> bool:
        host = (host or "").strip().lower().rstrip(".")
        return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)

    def meter(self, now: int, reason: str = "loop") -> int:
        """Charge elapsed time to auto-mode groups that had DNS activity.

        The query log is the only signal available: if a group's domains were
        resolved during the window, the group is considered "in use" for those
        seconds. On startup we only reset the cursor (never back-fill).
        """
        with self.store as conn:
            last = int(db.get_setting(conn, "meter_last_ts", "0") or 0)
            if reason == "startup" or last <= 0 or now - last < 5 or now - last > 300:
                db.set_setting(conn, "meter_last_ts", str(now))
                return 0
            window = now - last
            try:
                entries = self.adguard.querylog(limit=2000)
            except AdGuardError as exc:
                log.error("metering skipped: %s", exc)
                db.set_setting(conn, "meter_last_ts", str(now))
                return 0

            stamped = []
            for entry in entries:
                ts = parse_time(entry.get("time", ""))
                if ts is None or not (last < ts <= now):
                    continue
                client = entry.get("client") or ""
                if client in ("127.0.0.1", "::1"):
                    continue
                name = (entry.get("question") or {}).get("name") or ""
                stamped.append(name)
            charged = 0
            if stamped:
                day = budget.day_str(now, self.tz)
                for group in budget.list_groups(conn):
                    if not budget.is_permitted(conn, group, self.tz, now):
                        continue
                    suffixes = self.matcher(group)
                    if suffixes and any(self.host_matches(name, suffixes) for name in stamped):
                        cap = budget.budget_seconds(conn, group, self.tz, now)
                        budget.add_metered_seconds(conn, group.id, day, window, cap_seconds=cap)
                        charged += 1
            db.set_setting(conn, "meter_last_ts", str(now))
            return charged

    # -- individual halves, both idempotent ------------------------------
    def sync_services(self, now: int | None = None) -> list[str]:
        now = now or int(time.time())
        with self.store as conn:
            # Only services TimeFence itself took over are ever removed from the
            # AdGuard list; everything else the operator configured stays as-is.
            owned = budget.owned_services(conn)
            current = self.adguard.blocked_services()
            current_ids = list(current["ids"])
            wanted = {i for i in current_ids if i not in owned} | desired_services(
                conn, self.tz, now
            )
            if set(current_ids) == wanted:
                return []
            self.adguard.set_blocked_services(sorted(wanted), current["schedule"])
        log.info("blocked services: %s -> %s", sorted(current_ids), sorted(wanted))
        return sorted(wanted ^ set(current_ids))

    def sync_domains(self, now: int | None = None) -> bool:
        now = now or int(time.time())
        with self.store as conn:
            current = self.adguard.user_rules()
            desired = desired_domains(conn, self.tz, now)
            updated = build_rules(current, desired)
            if updated == current:
                return False
            self.adguard.set_user_rules(updated)
        log.info("user rules updated: %d managed domain(s) blocked", len(desired))
        return True

    # -- full pass -------------------------------------------------------
    def run(self, reason: str = "loop", now: int | None = None) -> dict:
        """Run every phase once.

        Lock order is reconcile lock, then store lock; taking the store lock
        first and calling into this method would deadlock, so it is refused.
        """
        if self.store.lock.held_by_current_thread():
            raise RuntimeError("reconciler.run() called while holding the store lock")
        with self._run_lock:
            return self._run(reason, now)

    def _run(self, reason: str = "loop", now: int | None = None) -> dict:
        now = now or int(time.time())
        result: dict = {"reason": reason, "changed": [], "errors": []}
        charged = self.meter(now, reason)
        if charged:
            result["metered_groups"] = charged
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
