"""Groups, budgets and sessions - the pure domain logic (no HTTP, no AdGuard)."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DATA_DIR = Path(__file__).parent / "data"

DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9_-]{1,63}(?<!-)(\.(?!-)[a-z0-9_-]{1,63}(?<!-))+$"
)


class ValidationError(ValueError):
    """User supplied something unusable."""


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #


def day_str(ts: int, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")


def local_midnight(ts: int, tz: ZoneInfo) -> int:
    d = datetime.fromtimestamp(ts, tz).date()
    return int(datetime(d.year, d.month, d.day, tzinfo=tz).timestamp())


def is_weekend(ts: int, tz: ZoneInfo) -> bool:
    return datetime.fromtimestamp(ts, tz).weekday() >= 5


def parse_hhmm(value: str, now: int, tz: ZoneInfo) -> int:
    """'21:00' -> epoch of the next occurrence (today if still in the future)."""
    try:
        hh_s, mm_s = value.strip().split(":")[:2]
        hh, mm = int(hh_s), int(mm_s)
    except (ValueError, AttributeError) as exc:
        raise ValidationError("time must be HH:MM") from exc
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValidationError("time must be HH:MM")
    local = datetime.fromtimestamp(now, tz).replace(hour=hh, minute=mm, second=0, microsecond=0)
    if local.timestamp() <= now:
        local += timedelta(days=1)
    return int(local.timestamp())


# --------------------------------------------------------------------------- #
# input normalisation
# --------------------------------------------------------------------------- #


def normalize_domain(value: str) -> str:
    """Accept 'https://WWW.Example.com/path?x' and return 'www.example.com'."""
    raw = (value or "").strip().lower()
    if not raw:
        raise ValidationError("empty domain")
    raw = raw.removeprefix("||").removesuffix("^")
    for prefix in ("https://", "http://", "//"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
    raw = raw.split("/")[0].split("?")[0].split("#")[0]
    raw = raw.split("@")[-1]  # strip userinfo
    if ":" in raw:  # strip port
        raw = raw.rsplit(":", 1)[0]
    raw = raw.strip(".")
    if raw.startswith("*."):
        raw = raw[2:]
    if not raw:
        raise ValidationError("empty domain")
    if not DOMAIN_RE.match(raw):
        raise ValidationError(f"not a valid domain: {value!r}")
    return raw


def normalize_domains(text: str) -> list[str]:
    """One per line; blanks and comments ignored; order preserved."""
    out: list[str] = []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        dom = normalize_domain(line)
        if dom not in out:
            out.append(dom)
    return out


def load_critical_patterns(path: Path | None = None) -> list[str]:
    file = path or (DATA_DIR / "critical_domains.txt")
    patterns: list[str] = []
    try:
        for line in file.read_text(encoding="utf-8").splitlines():
            line = line.strip().lower()
            if line and not line.startswith("#"):
                patterns.append(line)
    except FileNotFoundError:
        return []
    return patterns


def critical_match(domain: str, patterns: list[str]) -> str | None:
    """Returns the most specific pattern covering `domain` (subdomains included)."""
    dom = domain.lower().strip(".")
    best: str | None = None
    for pattern in patterns:
        candidate = pattern.strip().lower()
        if candidate.endswith("*"):
            hit = dom.startswith(candidate[:-1])
        else:
            hit = dom == candidate or dom.endswith("." + candidate)
        if hit and (best is None or len(candidate) > len(best)):
            best = pattern
    return best


# --------------------------------------------------------------------------- #
# groups
# --------------------------------------------------------------------------- #


@dataclass
class Group:
    id: int
    name: str
    emoji: str
    weekday_minutes: int
    weekend_minutes: int
    paused: bool
    deleted: bool
    services: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)

    def minutes_for(self, ts: int, tz: ZoneInfo) -> int:
        return self.weekend_minutes if is_weekend(ts, tz) else self.weekday_minutes


def _group_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> Group:
    services = [
        r["service_id"]
        for r in conn.execute(
            "SELECT service_id FROM group_services WHERE group_id=? ORDER BY service_id",
            (row["id"],),
        )
    ]
    domains = [
        r["domain"]
        for r in conn.execute(
            "SELECT domain FROM group_domains WHERE group_id=? ORDER BY domain", (row["id"],)
        )
    ]
    return Group(
        id=row["id"],
        name=row["name"],
        emoji=row["emoji"],
        weekday_minutes=row["weekday_minutes"],
        weekend_minutes=row["weekend_minutes"],
        paused=bool(row["paused"]),
        deleted=bool(row["deleted"]),
        services=services,
        domains=domains,
    )


def list_groups(conn: sqlite3.Connection, include_deleted: bool = False) -> list[Group]:
    sql = (
        "SELECT * FROM groups" + ("" if include_deleted else " WHERE deleted=0") + " ORDER BY name"
    )
    return [_group_from_row(conn, row) for row in conn.execute(sql)]


def get_group(conn: sqlite3.Connection, group_id: int) -> Group | None:
    row = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    return _group_from_row(conn, row) if row else None


def _assert_unique(
    conn: sqlite3.Connection, services: list[str], domains: list[str], gid: int | None
) -> None:
    for service in services:
        row = conn.execute(
            "SELECT g.id, g.name FROM group_services s JOIN groups g ON g.id=s.group_id "
            "WHERE s.service_id=? AND g.deleted=0 AND s.group_id IS NOT ?",
            (service, gid),
        ).fetchone()
        if row:
            raise ValidationError(f"service {service!r} is already used by group {row['name']!r}")
    for domain in domains:
        row = conn.execute(
            "SELECT g.id, g.name FROM group_domains d JOIN groups g ON g.id=d.group_id "
            "WHERE d.domain=? AND g.deleted=0 AND d.group_id IS NOT ?",
            (domain, gid),
        ).fetchone()
        if row:
            raise ValidationError(f"domain {domain!r} is already used by group {row['name']!r}")


def owned_services(conn: sqlite3.Connection) -> set[str]:
    """Services TimeFence has taken ownership of (even if their group is gone)."""
    return {row["service_id"] for row in conn.execute("SELECT service_id FROM owned_services")}


def mark_owned(conn: sqlite3.Connection, services: list[str]) -> None:
    now = int(time.time())
    conn.executemany(
        "INSERT OR IGNORE INTO owned_services(service_id, added_at) VALUES(?,?)",
        [(s, now) for s in services],
    )


def _write_links(
    conn: sqlite3.Connection, gid: int, services: list[str], domains: list[str]
) -> None:
    conn.execute("DELETE FROM group_services WHERE group_id=?", (gid,))
    conn.execute("DELETE FROM group_domains WHERE group_id=?", (gid,))
    conn.executemany(
        "INSERT INTO group_services(group_id, service_id) VALUES(?,?)",
        [(gid, s) for s in services],
    )
    conn.executemany(
        "INSERT INTO group_domains(group_id, domain) VALUES(?,?)",
        [(gid, d) for d in domains],
    )


def create_group(
    conn: sqlite3.Connection,
    *,
    name: str,
    emoji: str = "",
    weekday_minutes: int,
    weekend_minutes: int | None = None,
    services: list[str] | None = None,
    domains: list[str] | None = None,
    paused: bool = False,
    now: int | None = None,
) -> Group:
    name = (name or "").strip()
    if not name:
        raise ValidationError("name is required")
    if len(name) > 60:
        raise ValidationError("name is too long")
    services = [s.strip().lower() for s in (services or []) if s.strip()]
    domains = [normalize_domain(d) for d in (domains or [])]
    domains = list(dict.fromkeys(domains))
    weekend = weekday_minutes if weekend_minutes is None else weekend_minutes
    for value in (weekday_minutes, weekend):
        if not (0 <= int(value) <= 1440):
            raise ValidationError("minutes must be between 0 and 1440")
    if not services and not domains:
        raise ValidationError("a group needs at least one service or domain")
    _assert_unique(conn, services, domains, None)
    cur = conn.execute(
        "INSERT INTO groups(name, emoji, weekday_minutes, weekend_minutes, paused, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (
            name,
            emoji.strip()[:8],
            int(weekday_minutes),
            int(weekend),
            int(paused),
            now or int(time.time()),
        ),
    )
    gid = int(cur.lastrowid)
    _write_links(conn, gid, services, domains)
    mark_owned(conn, services)
    conn.commit()
    return get_group(conn, gid)  # type: ignore[return-value]


def update_group(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    name: str | None = None,
    emoji: str | None = None,
    weekday_minutes: int | None = None,
    weekend_minutes: int | None = None,
    services: list[str] | None = None,
    domains: list[str] | None = None,
    paused: bool | None = None,
) -> Group:
    group = get_group(conn, group_id)
    if not group or group.deleted:
        raise ValidationError("group not found")

    new_name = group.name if name is None else name.strip()
    if not new_name:
        raise ValidationError("name is required")
    dup = conn.execute(
        "SELECT id FROM groups WHERE name=? AND deleted=0 AND id IS NOT ?", (new_name, group_id)
    ).fetchone()
    if dup:
        raise ValidationError(f"another group is already called {new_name!r}")

    new_weekday = group.weekday_minutes if weekday_minutes is None else int(weekday_minutes)
    new_weekend = group.weekend_minutes if weekend_minutes is None else int(weekend_minutes)
    for value in (new_weekday, new_weekend):
        if not (0 <= value <= 1440):
            raise ValidationError("minutes must be between 0 and 1440")

    new_services = (
        group.services if services is None else [s.strip().lower() for s in services if s.strip()]
    )
    new_domains = (
        group.domains
        if domains is None
        else list(dict.fromkeys(normalize_domain(d) for d in domains))
    )
    if not new_services and not new_domains:
        raise ValidationError("a group needs at least one service or domain")
    _assert_unique(conn, new_services, new_domains, group_id)

    conn.execute(
        "UPDATE groups SET name=?, emoji=?, weekday_minutes=?, weekend_minutes=?, "
        "paused=? WHERE id=?",
        (
            new_name,
            (group.emoji if emoji is None else emoji.strip()[:8]),
            new_weekday,
            new_weekend,
            int(group.paused if paused is None else paused),
            group_id,
        ),
    )
    _write_links(conn, group_id, new_services, new_domains)
    mark_owned(conn, new_services)
    conn.commit()
    return get_group(conn, group_id)  # type: ignore[return-value]


def delete_group(conn: sqlite3.Connection, group_id: int) -> str:
    """Soft delete: history stays, the group no longer manages anything."""
    group = get_group(conn, group_id)
    if not group:
        raise ValidationError("group not found")
    conn.execute("UPDATE groups SET deleted=1, paused=1 WHERE id=?", (group_id,))
    conn.execute("DELETE FROM group_services WHERE group_id=?", (group_id,))
    conn.execute("DELETE FROM group_domains WHERE group_id=?", (group_id,))
    conn.execute("DELETE FROM free_allow WHERE group_id=?", (group_id,))
    conn.commit()
    return group.name


# --------------------------------------------------------------------------- #
# budgets and sessions
# --------------------------------------------------------------------------- #


def bonus_seconds(conn: sqlite3.Connection, gid: int, day: str) -> int:
    row = conn.execute(
        "SELECT seconds FROM bonus WHERE day=? AND group_id=?", (day, gid)
    ).fetchone()
    return int(row["seconds"]) if row else 0


def active_session(conn: sqlite3.Connection, gid: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sessions WHERE group_id=? AND ended_at IS NULL "
        "ORDER BY started_at DESC LIMIT 1",
        (gid,),
    ).fetchone()


def used_seconds(
    conn: sqlite3.Connection, gid: int, day: str, tz: ZoneInfo, now: int | None = None
) -> int:
    now = now or int(time.time())
    total = 0
    for row in conn.execute(
        "SELECT started_at, ended_at, planned_seconds, used_seconds FROM sessions WHERE group_id=?",
        (gid,),
    ):
        if day_str(row["started_at"], tz) != day:
            continue
        if row["ended_at"] is None:
            total += min(row["planned_seconds"], max(0, now - row["started_at"]))
        else:
            total += row["used_seconds"]
    return total


def budget_seconds(
    conn: sqlite3.Connection, group: Group, tz: ZoneInfo, now: int | None = None
) -> int:
    now = now or int(time.time())
    day = day_str(now, tz)
    return group.minutes_for(now, tz) * 60 + bonus_seconds(conn, group.id, day)


def remaining_seconds(
    conn: sqlite3.Connection, group: Group, tz: ZoneInfo, now: int | None = None
) -> int:
    now = now or int(time.time())
    day = day_str(now, tz)
    return max(0, budget_seconds(conn, group, tz, now) - used_seconds(conn, group.id, day, tz, now))


def free_until(conn: sqlite3.Connection, gid: int) -> int | None:
    row = conn.execute("SELECT until_ts FROM free_allow WHERE group_id=?", (gid,)).fetchone()
    return int(row["until_ts"]) if row else None


def is_permitted(
    conn: sqlite3.Connection, group: Group, tz: ZoneInfo, now: int | None = None
) -> bool:
    now = now or int(time.time())
    if group.paused or group.deleted:
        return False
    until = free_until(conn, group.id)
    if until and until > now:
        return True
    session = active_session(conn, group.id)
    return bool(session and session["started_at"] + session["planned_seconds"] > now)


def start_session(
    conn: sqlite3.Connection,
    group: Group,
    minutes: int,
    tz: ZoneInfo,
    *,
    actor: str,
    now: int | None = None,
    options: tuple[int, ...] = (15, 30, 60),
) -> sqlite3.Row:
    now = now or int(time.time())
    if group.paused:
        raise ValidationError("this group is paused")
    minutes = int(minutes)
    if minutes <= 0 or minutes > 1440:
        raise ValidationError("minutes out of range")
    if minutes not in options and minutes > max(options):
        raise ValidationError("unsupported session length")
    if is_permitted(conn, group, tz, now):
        raise ValidationError("a session is already running (or the group is in free-allow mode)")
    remaining = remaining_seconds(conn, group, tz, now)
    if remaining < 60:
        raise ValidationError("no minutes left today")
    planned = min(minutes * 60, remaining)
    planned = max(60, (planned // 60) * 60)
    conn.execute(
        "INSERT INTO sessions(group_id, planned_seconds, started_at, actor) VALUES(?,?,?,?)",
        (group.id, planned, now, actor),
    )
    conn.commit()
    return conn.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 1").fetchone()  # type: ignore[return-value]


def close_session(
    conn: sqlite3.Connection, session_id: int, reason: str, now: int | None = None
) -> sqlite3.Row | None:
    now = now or int(time.time())
    row = conn.execute(
        "SELECT * FROM sessions WHERE id=? AND ended_at IS NULL", (session_id,)
    ).fetchone()
    if not row:
        return None
    if reason == "expired":
        used = row["planned_seconds"]
        ended = row["started_at"] + row["planned_seconds"]
    else:
        used = max(0, min(row["planned_seconds"], now - row["started_at"]))
        ended = now
    conn.execute(
        "UPDATE sessions SET ended_at=?, used_seconds=?, end_reason=? WHERE id=?",
        (ended, used, reason, session_id),
    )
    conn.commit()
    return conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()


def stop_sessions(
    conn: sqlite3.Connection, group_id: int, reason: str, now: int | None = None
) -> list[sqlite3.Row]:
    rows = list(
        conn.execute("SELECT id FROM sessions WHERE group_id=? AND ended_at IS NULL", (group_id,))
    )
    stopped = []
    for row in rows:
        closed = close_session(conn, row["id"], reason, now)
        if closed:
            stopped.append(closed)
    return stopped


def close_expired(conn: sqlite3.Connection, now: int | None = None) -> list[sqlite3.Row]:
    now = now or int(time.time())
    closed = []
    for row in list(
        conn.execute("SELECT id, started_at, planned_seconds FROM sessions WHERE ended_at IS NULL")
    ):
        if row["started_at"] + row["planned_seconds"] <= now:
            result = close_session(conn, row["id"], "expired", now)
            if result:
                closed.append(result)
    return closed


def add_bonus(
    conn: sqlite3.Connection, gid: int, minutes: int, tz: ZoneInfo, now: int | None = None
) -> int:
    now = now or int(time.time())
    if not -1440 <= int(minutes) <= 1440:
        raise ValidationError("bonus minutes out of range")
    day = day_str(now, tz)
    conn.execute(
        "INSERT INTO bonus(day, group_id, seconds) VALUES(?,?,?) "
        "ON CONFLICT(day, group_id) DO UPDATE SET seconds=bonus.seconds+excluded.seconds",
        (day, gid, int(minutes) * 60),
    )
    conn.commit()
    return bonus_seconds(conn, gid, day)


def set_free_until(conn: sqlite3.Connection, gid: int, until_ts: int) -> None:
    conn.execute(
        "INSERT INTO free_allow(group_id, until_ts) VALUES(?,?) "
        "ON CONFLICT(group_id) DO UPDATE SET until_ts=excluded.until_ts",
        (gid, int(until_ts)),
    )
    conn.commit()


def clear_free(conn: sqlite3.Connection, gid: int) -> None:
    conn.execute("DELETE FROM free_allow WHERE group_id=?", (gid,))
    conn.commit()


def history(
    conn: sqlite3.Connection, tz: ZoneInfo, days: int = 7, now: int | None = None
) -> list[dict]:
    now = now or int(time.time())
    start = local_midnight(now, tz) - (days - 1) * 86400
    groups = {g.id: g for g in list_groups(conn, include_deleted=True)}
    rows = list(
        conn.execute(
            "SELECT group_id, started_at, ended_at, planned_seconds, used_seconds "
            "FROM sessions WHERE started_at>=?",
            (start,),
        )
    )
    out = []
    for offset in range(days):
        ts = start + offset * 86400
        day = day_str(ts, tz)
        entry: dict[str, float | str] = {"day": day}
        for gid in groups:
            entry[str(gid)] = 0.0
        for row in rows:
            if day_str(row["started_at"], tz) != day:
                continue
            used = (
                row["used_seconds"]
                if row["ended_at"] is not None
                else min(row["planned_seconds"], max(0, now - row["started_at"]))
            )
            key = str(row["group_id"])
            if key in entry:
                entry[key] = float(entry[key]) + used / 60
        for key in list(entry):
            if key != "day":
                entry[key] = round(float(entry[key]), 1)
        out.append(entry)
    return out


def sessions_today(conn: sqlite3.Connection, tz: ZoneInfo, now: int | None = None) -> list[dict]:
    now = now or int(time.time())
    start = local_midnight(now, tz)
    groups = {g.id: g for g in list_groups(conn, include_deleted=True)}
    out = []
    for row in conn.execute(
        "SELECT * FROM sessions WHERE started_at>=? ORDER BY started_at DESC", (start,)
    ):
        group = groups.get(row["group_id"])
        used = (
            row["used_seconds"]
            if row["ended_at"] is not None
            else min(row["planned_seconds"], max(0, now - row["started_at"]))
        )
        out.append(
            {
                "id": row["id"],
                "group": group.name if group else f"#{row['group_id']}",
                "emoji": group.emoji if group else "",
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "planned_minutes": row["planned_seconds"] // 60,
                "used_minutes": round(used / 60, 1),
                "end_reason": row["end_reason"],
                "actor": row["actor"],
            }
        )
    return out


def log_event(conn: sqlite3.Connection, kind: str, detail: dict | str = "") -> None:
    conn.execute(
        "INSERT INTO events(ts, kind, detail) VALUES(?,?,?)",
        (int(time.time()), kind, json.dumps(detail) if isinstance(detail, dict) else detail),
    )
    conn.commit()


def group_state(
    conn: sqlite3.Connection, group: Group, tz: ZoneInfo, now: int | None = None
) -> dict:
    now = now or int(time.time())
    day = day_str(now, tz)
    session = active_session(conn, group.id)
    until = free_until(conn, group.id)
    permitted = is_permitted(conn, group, tz, now)
    return {
        "id": group.id,
        "name": group.name,
        "emoji": group.emoji,
        "weekday_minutes": group.weekday_minutes,
        "weekend_minutes": group.weekend_minutes,
        "minutes_today": group.minutes_for(now, tz),
        "paused": group.paused,
        "services": group.services,
        "domains": group.domains,
        "bonus_minutes": round(bonus_seconds(conn, group.id, day) / 60, 1),
        "used_minutes": round(used_seconds(conn, group.id, day, tz, now) / 60, 1),
        "remaining_minutes": round(remaining_seconds(conn, group, tz, now) / 60, 1),
        "permitted": permitted,
        "free_until": until if until and until > now else None,
        "active_session": (
            {
                "id": session["id"],
                "ends_at": session["started_at"] + session["planned_seconds"],
                "started_at": session["started_at"],
                "planned_minutes": session["planned_seconds"] // 60,
            }
            if session
            else None
        ),
    }


def today(tz: ZoneInfo, now: int | None = None) -> date:
    return datetime.fromtimestamp(now or int(time.time()), tz).date()
