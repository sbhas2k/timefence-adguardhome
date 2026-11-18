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
    rules: list[str],
    begin: str = SECTION_BEGIN,
    end: str = SECTION_END,
    damaged: set[str] | None = None,
) -> list[str]:
    """Return the rules with the managed section (and its markers) removed.

    Only a begin/end pair is removed. An unbalanced marker is dropped on its
    own and everything it might have covered stays: deleting a whole tail
    because one marker is missing would throw away somebody else's rules.
    """
    markers: list[tuple[int, str]] = []
    for index, line in enumerate(rules):
        stripped = line.strip()
        if stripped == begin:
            markers.append((index, "begin"))
        elif stripped == end:
            markers.append((index, "end"))

    skip: set[int] = set()
    open_at: int | None = None
    unbalanced = False
    for index, kind in markers:
        if kind == "begin":
            if open_at is not None:
                skip.add(open_at)  # its body stays as a foreign rule
                unbalanced = True
            open_at = index
        elif open_at is None:
            skip.add(index)  # a stray end
            unbalanced = True
        else:
            skip.update(range(open_at, index + 1))
            open_at = None
    if open_at is not None:
        skip.add(open_at)
        unbalanced = True
    if unbalanced and damaged is not None:
        damaged.add("rules_section_damaged")
    return [line for index, line in enumerate(rules) if index not in skip]


def strip_legacy_sections(rules: list[str], damaged: set[str] | None = None) -> list[str]:
    for marker in LEGACY_MARKERS:
        rules = strip_section(rules, f"# {marker}:begin", f"# {marker}:end", damaged)
    return rules


def build_rules(
    current: list[str], desired_domains: list[str], damaged: set[str] | None = None
) -> list[str]:
    """Replace our section in `current`, preserving every foreign line verbatim."""
    base = strip_legacy_sections(strip_section(current, damaged=damaged), damaged)
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
        self.last_ok_ts: float | None = None
        self.warnings: set[str] = set()
        self.stopping = threading.Event()
        self.closed = False
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
        # AdGuard calls happen outside the store lock
        try:
            entries = self.adguard.querylog(limit=2000)
        except AdGuardError as exc:
            log.error("metering skipped: %s", exc)
            with self.store as conn:
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
        if stamped:
            self.service_domains()  # warm the matcher cache outside the lock
        charged = 0
        with self.store as conn:
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
            wanted = desired_services(conn, self.tz, now)
        current = self.adguard.blocked_services()
        current_ids = list(current["ids"])
        wanted = {i for i in current_ids if i not in owned} | wanted
        if set(current_ids) == wanted:
            return []
        if self.stopping.is_set():
            return []
        self.adguard.set_blocked_services(sorted(wanted), current["schedule"])
        log.info("blocked services: %s -> %s", sorted(current_ids), sorted(wanted))
        return sorted(wanted ^ set(current_ids))

    def sync_domains(self, now: int | None = None) -> bool:
        now = now or int(time.time())
        with self.store as conn:
            desired = desired_domains(conn, self.tz, now)
        current = self.adguard.user_rules()
        damaged: set[str] = set()
        updated = build_rules(current, desired, damaged)
        if damaged:
            self.warnings.add("rules_section_damaged")
        else:
            self.warnings.discard("rules_section_damaged")
        if updated == current:
            return False
        if self.stopping.is_set():
            return False
        self.adguard.set_user_rules(updated)
        log.info("user rules updated: %d managed domain(s) blocked", len(desired))
        return True

    # -- full pass -------------------------------------------------------
    def run(self, reason: str = "loop", now: int | None = None, wait: float | None = None) -> dict:
        """Run every phase once.

        Lock order is reconcile lock, then store lock; taking the store lock
        first and calling into this method would deadlock, so it is refused.
        `wait` bounds how long a caller (a request handler) waits for a run
        that is already in progress; None waits as long as it takes.
        """
        if self.closed:
            return {"reason": reason, "changed": [], "errors": [], "closed": True}
        if self.store.lock.held_by_current_thread():
            raise RuntimeError("reconciler.run() called while holding the store lock")
        if wait is None:
            acquired = self._run_lock.acquire()
        else:
            acquired = self._run_lock.acquire(timeout=wait)
        if not acquired:
            log.warning("reconcile run skipped: a previous run is still in progress")
            return {"reason": reason, "changed": [], "errors": [], "pending": True}
        try:
            if self.closed:
                return {"reason": reason, "changed": [], "errors": [], "closed": True}
            return self._run(reason, now)
        finally:
            self._run_lock.release()

    def _run(self, reason: str = "loop", now: int | None = None) -> dict:
        now = now or int(time.time())
        result: dict = {"reason": reason, "changed": [], "errors": []}
        synced = True
        if not self.stopping.is_set():
            try:
                charged = self.meter(now, reason)
                if charged:
                    result["metered_groups"] = charged
            except AdGuardError as exc:
                log.error("metering failed: %s", exc)
                result["errors"].append(str(exc))
            except Exception as exc:  # noqa: BLE001 - one phase must not stop the rest
                log.exception("metering failed")
                result["errors"].append(f"internal error: {type(exc).__name__}")
        if not self.stopping.is_set():
            try:
                changed_services = self.sync_services(now)
                if changed_services:
                    result["changed"].append("services")
            except AdGuardError as exc:
                log.error("service sync failed: %s", exc)
                result["errors"].append(str(exc))
                synced = False
            except Exception as exc:  # noqa: BLE001 - one phase must not stop the rest
                log.exception("service sync failed")
                result["errors"].append(f"internal error: {type(exc).__name__}")
                synced = False
        if not self.stopping.is_set():
            try:
                if self.sync_domains(now):
                    result["changed"].append("domains")
            except AdGuardError as exc:
                log.error("domain sync failed: %s", exc)
                result["errors"].append(str(exc))
                synced = False
            except Exception as exc:  # noqa: BLE001 - one phase must not stop the rest
                log.exception("domain sync failed")
                result["errors"].append(f"internal error: {type(exc).__name__}")
                synced = False
        if synced and not self.stopping.is_set():
            self.last_ok_ts = time.time()
        self.last_error = result["errors"][0] if result["errors"] else None
        self.last_result = result
        return result

    # -- hands-on paths: shutdown blocking and release -------------------
    def block_all(self) -> dict:
        """Block every managed group; used when TimeFence shuts down.

        Never reads `stopping` or `closed` as a reason to skip work: this is
        the last write of the process.
        """
        acquired = self._run_lock.acquire(timeout=5)
        self.closed = True
        if not acquired:
            log.error("shutdown: a reconcile run is still busy, blocks not applied")
            return {"changed": [], "errors": ["reconcile run still busy"]}
        try:
            changed: list[str] = []
            with self.store as conn:
                owned = budget.owned_services(conn)
                groups = budget.list_groups(conn)
                services = {service for group in groups for service in group.services}
                domains = sorted({domain for group in groups for domain in group.domains})
            current = self.adguard.blocked_services()
            current_ids = list(current["ids"])
            wanted = {service for service in current_ids if service not in owned} | services
            if set(current_ids) != wanted:
                self.adguard.set_blocked_services(sorted(wanted), current["schedule"])
                changed.append("services")
            current_rules = self.adguard.user_rules()
            updated = build_rules(current_rules, domains)
            if updated != current_rules:
                self.adguard.set_user_rules(updated)
                changed.append("domains")
            return {"changed": changed, "errors": []}
        except Exception as exc:  # noqa: BLE001 - shutdown must never raise
            log.exception("shutdown: blocking every group failed")
            return {"changed": [], "errors": [str(exc)]}
        finally:
            self._run_lock.release()

    def release(self) -> dict:
        """Remove TimeFence's services and rules from AdGuard Home.

        Foreign services and foreign rules stay; only what TimeFence took over
        is lifted.
        """
        with self._run_lock:
            with self.store as conn:
                owned = budget.owned_services(conn)
            current = self.adguard.blocked_services()
            current_ids = list(current["ids"])
            services = {service for service in current_ids if service not in owned}
            removed = sorted(service for service in current_ids if service in owned)
            if set(current_ids) != services:
                self.adguard.set_blocked_services(sorted(services), current["schedule"])
            current_rules = self.adguard.user_rules()
            wanted_rules = build_rules(current_rules, [])
            rules_removed = max(0, len(current_rules) - len(wanted_rules))
            if wanted_rules != current_rules:
                self.adguard.set_user_rules(wanted_rules)
            return {"services": removed, "rules_removed": rules_removed}
