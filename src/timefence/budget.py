"""Groups, their allowed windows and the state that follows - pure domain logic.

No HTTP, no AdGuard: everything here works on a database connection and a clock,
so the same rules decide what the API shows, what a kid account sees and what
the reconciler writes to AdGuard.
"""

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
CLOCK_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")

WINDOW_LIMIT = 4
WINDOW_DAYS = ("all", "weekday", "weekend")
PERMITTED_STATUSES = frozenset({"allowed", "free"})
# How far ahead the next opening/closing is looked for. A weekly schedule always
# flips inside this window; a schedule that covers every minute never does.
SCHEDULE_HORIZON_DAYS = 8


class ValidationError(ValueError):
    """User supplied something unusable."""


class WindowError(ValidationError):
    """An allowed-time window cannot be used as given."""


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #


def next_local_midnight(ts: int, tz: ZoneInfo) -> int:
    """Start of the next local day; never `ts + 86400` (DST days are 23 or 25 hours)."""
    following = datetime.fromtimestamp(ts, tz).date() + timedelta(days=1)
    return int(datetime(following.year, following.month, following.day, tzinfo=tz).timestamp())


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


def clock_text(minutes: int) -> str:
    """Minutes since midnight -> zero-padded 'HH:MM'."""
    return f"{int(minutes) // 60:02d}:{int(minutes) % 60:02d}"


def clock_minutes(value: object) -> int:
    """'HH:MM' -> minutes since midnight; anything else is refused."""
    match = CLOCK_RE.match(str(value if value is not None else "").strip())
    if not match:
        raise WindowError("a window time must be HH:MM")
    return int(match.group(1)) * 60 + int(match.group(2))


def clock_str(ts: int, tz: ZoneInfo) -> str:
    """Epoch -> zero-padded local 'HH:MM'."""
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M")


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
# groups and their allowed windows
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Window:
    """An allowed window: minutes since local midnight, on days the window starts.

    The day filter belongs to the day the window starts on, so a window that
    wraps midnight still runs into the morning after a matching start day.
    """

    start_min: int
    end_min: int
    days: str = "all"

    def as_dict(self) -> dict[str, str]:
        return {
            "start": clock_text(self.start_min),
            "end": clock_text(self.end_min),
            "days": self.days,
        }


@dataclass
class Group:
    id: int
    name: str
    emoji: str
    paused: bool
    deleted: bool
    force_block_until: int = 0  # admin "Block now" until midnight
    owner_id: int | None = None  # user this group belongs to (None = shared)
    services: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)


def normalize_windows(items: object) -> list[Window]:
    """Validate the wire format and return usable windows (at most four)."""
    if items is None:
        return []
    if not isinstance(items, list | tuple):
        raise WindowError("windows must be a list")
    if len(items) > WINDOW_LIMIT:
        raise WindowError(f"at most {WINDOW_LIMIT} time windows")
    out: list[Window] = []
    for item in items:
        if isinstance(item, Window):
            out.append(item)
            continue
        if not isinstance(item, dict):
            raise WindowError("each window needs a start, an end and days")
        start_min = clock_minutes(item.get("start"))
        end_min = clock_minutes(item.get("end"))
        if start_min == end_min:
            raise WindowError("a window must start and end at different times")
        days = str(item.get("days") or "all").strip().lower()
        if days not in WINDOW_DAYS:
            raise WindowError("days must be all, weekday or weekend")
        out.append(Window(start_min, end_min, days))
    return out


def days_match(days: str, weekday: int) -> bool:
    """`weekday` is 0 (Monday) .. 6 (Sunday)."""
    if days == "weekday":
        return weekday < 5
    if days == "weekend":
        return weekday >= 5
    return True


def window_active(window: Window, ts: int, tz: ZoneInfo) -> bool:
    """Is the window open at `ts`? Judged on the local wall clock.

    The day filter belongs to the day the window starts on, so a window that
    wraps midnight still ends on the morning after a matching start day.
    """
    local = datetime.fromtimestamp(ts, tz)
    minute = local.hour * 60 + local.minute
    if window.start_min < window.end_min:
        return window.start_min <= minute < window.end_min and days_match(
            window.days, local.weekday()
        )
    if days_match(window.days, local.weekday()) and minute >= window.start_min:
        return True
    previous_day = (local.date() - timedelta(days=1)).weekday()
    return days_match(window.days, previous_day) and minute < window.end_min


def active_window(group: Group, ts: int, tz: ZoneInfo) -> Window | None:
    for window in group.windows:
        if window_active(window, ts, tz):
            return window
    return None


def _offset(ts: int, tz: ZoneInfo) -> timedelta | None:
    return datetime.fromtimestamp(ts, tz).utcoffset()


def _day_start(day: date, tz: ZoneInfo) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=tz).timestamp())


def _local_instant(day: date, minute_of_day: int, tz: ZoneInfo, fold: int) -> int:
    """Epoch for a local wall time; a skipped time resolves to the gap edge."""
    return int(
        datetime(
            day.year,
            day.month,
            day.day,
            minute_of_day // 60,
            minute_of_day % 60,
            tzinfo=tz,
            fold=fold,
        ).timestamp()
    )


def _transition_instant(day_start: int, next_start: int, tz: ZoneInfo) -> int:
    """The first minute boundary in the local day whose UTC offset differs.

    Only called when the local day is not 24 hours long, so there is exactly one
    ordinary transition to find; a shorter or longer day already proves it.
    """
    base = _offset(day_start, tz)
    low, high = day_start, next_start
    while high - low > 60:
        middle = low + ((high - low) // 120) * 60
        if middle <= low:
            break
        if _offset(middle, tz) == base:
            low = middle
        else:
            high = middle
    return high


def _flip_candidates(group: Group, tz: ZoneInfo, start: int) -> list[int]:
    """Every instant in the horizon where the open/closed answer could change.

    A window changes its mind when the local wall clock reaches its start or end
    (two instants on a day whose clock repeats, possibly none when the clock
    skips that wall time). A DST transition can flip the answer by itself, for a
    window that spans the skipped or repeated hour, so it is a candidate too.
    Stepping every minute for eight days would be 11,520 steps per group; these
    candidates are at most ~160 and the fast path is proved against the oracle.
    """
    start_date = datetime.fromtimestamp(start, tz).date()
    candidates: set[int] = set()
    for offset in range(SCHEDULE_HORIZON_DAYS + 1):
        day = start_date + timedelta(days=offset)
        day_start = _day_start(day, tz)
        candidates.add(day_start)
        next_start = _day_start(day + timedelta(days=1), tz)
        if next_start - day_start != 86400:
            candidates.add(_transition_instant(day_start, next_start, tz))
        for window in group.windows:
            for minute_of_day in (window.start_min, window.end_min):
                for fold in (0, 1):
                    candidates.add(_local_instant(day, minute_of_day, tz, fold))
    limit = start + SCHEDULE_HORIZON_DAYS * 86400
    return sorted(instant for instant in candidates if instant <= limit)


def schedule_state(group: Group, tz: ZoneInfo, now: int | None = None) -> tuple[bool, int | None]:
    """(is the schedule open now, the minute it next flips or None).

    A group with no windows is always open and never flips. Otherwise the next
    flip is the first whole minute at or after `now` where the open answer
    differs; it is found from candidate edges, never by building a local wall
    time for the flip, which would silently pick a side on a DST day.
    """
    now = int(now if now is not None else time.time())
    if not group.windows:
        return True, None
    current = active_window(group, now, tz) is not None
    start = now - (now % 60)
    for instant in _flip_candidates(group, tz, start):
        if instant <= start:
            continue
        if (active_window(group, instant, tz) is not None) != current:
            return current, instant
    return current, None


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
    windows = [
        Window(int(r["start_min"]), int(r["end_min"]), r["days"])
        for r in conn.execute(
            "SELECT start_min, end_min, days FROM group_windows "
            "WHERE group_id=? ORDER BY start_min, id",
            (row["id"],),
        )
    ]
    return Group(
        id=row["id"],
        name=row["name"],
        emoji=row["emoji"],
        paused=bool(row["paused"]),
        deleted=bool(row["deleted"]),
        force_block_until=int(row["force_block_until"] or 0),
        owner_id=row["owner_id"],
        services=services,
        domains=domains,
        windows=windows,
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


def _write_windows(conn: sqlite3.Connection, gid: int, windows: list[Window]) -> None:
    """Replace a group's allowed windows; part of the group's own transaction."""
    conn.execute("DELETE FROM group_windows WHERE group_id=?", (gid,))
    conn.executemany(
        "INSERT INTO group_windows(group_id, start_min, end_min, days) VALUES(?,?,?,?)",
        [(gid, w.start_min, w.end_min, w.days) for w in windows],
    )


def create_group(
    conn: sqlite3.Connection,
    *,
    name: str,
    emoji: str = "",
    services: list[str] | None = None,
    domains: list[str] | None = None,
    windows: list[dict] | None = None,
    paused: bool = False,
    owner_id: int | None = None,
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
    allowed = normalize_windows(windows)
    if not services and not domains:
        raise ValidationError("a group needs at least one service or domain")
    _assert_unique(conn, services, domains, None)
    cur = conn.execute(
        "INSERT INTO groups(name, emoji, paused, owner_id, created_at) VALUES(?,?,?,?,?)",
        (
            name,
            emoji.strip()[:8],
            int(paused),
            owner_id,
            now or int(time.time()),
        ),
    )
    gid = int(cur.lastrowid)
    _write_links(conn, gid, services, domains)
    _write_windows(conn, gid, allowed)
    mark_owned(conn, services)
    conn.commit()
    return get_group(conn, gid)  # type: ignore[return-value]


def update_group(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    name: str | None = None,
    emoji: str | None = None,
    services: list[str] | None = None,
    domains: list[str] | None = None,
    windows: list[dict] | None = None,
    paused: bool | None = None,
    owner_id: int | None = None,
    set_owner: bool = False,
) -> Group:
    group = get_group(conn, group_id)
    if not group or group.deleted:
        raise ValidationError("group not found")

    new_name = group.name if name is None else name.strip()
    if not new_name:
        raise ValidationError("name is required")
    if len(new_name) > 60:
        raise ValidationError("name is too long")
    dup = conn.execute(
        "SELECT id FROM groups WHERE name=? AND deleted=0 AND id IS NOT ?", (new_name, group_id)
    ).fetchone()
    if dup:
        raise ValidationError(f"another group is already called {new_name!r}")

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
    new_windows = group.windows if windows is None else normalize_windows(windows)

    conn.execute(
        "UPDATE groups SET name=?, emoji=?, paused=?, owner_id=? WHERE id=?",
        (
            new_name,
            (group.emoji if emoji is None else emoji.strip()[:8]),
            int(group.paused if paused is None else paused),
            (owner_id if set_owner else group.owner_id),
            group_id,
        ),
    )
    _write_links(conn, group_id, new_services, new_domains)
    _write_windows(conn, group_id, new_windows)
    mark_owned(conn, new_services)
    conn.commit()
    return get_group(conn, group_id)  # type: ignore[return-value]


def delete_group(conn: sqlite3.Connection, group_id: int) -> str:
    """Soft delete: the group no longer manages anything, its name is freed."""
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
# free time, admin blocks and the one status predicate
# --------------------------------------------------------------------------- #


def free_until(conn: sqlite3.Connection, gid: int) -> int | None:
    row = conn.execute("SELECT until_ts FROM free_allow WHERE group_id=?", (gid,)).fetchone()
    return int(row["until_ts"]) if row else None


def set_force_block(conn: sqlite3.Connection, gid: int, until_ts: int) -> None:
    conn.execute("UPDATE groups SET force_block_until=? WHERE id=?", (int(until_ts), gid))
    conn.commit()


def clear_force_block(conn: sqlite3.Connection, gid: int) -> None:
    conn.execute("UPDATE groups SET force_block_until=0 WHERE id=?", (gid,))
    conn.commit()


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


def group_status(
    conn: sqlite3.Connection,
    group: Group,
    tz: ZoneInfo,
    now: int | None = None,
    scheduled: tuple[bool, int | None] | None = None,
) -> tuple[str, int | None]:
    """One status per group, in the same order as `is_permitted`.

    paused/deleted > free > blocked_admin > outside_hours > allowed. "Free
    until" disables the allowed windows for as long as it lasts, and an admin
    block wins over an open window.
    """
    now = int(now if now is not None else time.time())
    if group.paused or group.deleted:
        return "paused", None
    until = free_until(conn, group.id)
    if until and until > now:
        return "free", None
    if group.force_block_until and group.force_block_until > now:
        return "blocked_admin", group.force_block_until
    open_now, change_ts = scheduled if scheduled is not None else schedule_state(group, tz, now)
    if not open_now:
        return "outside_hours", change_ts
    return "allowed", None


def is_permitted(
    conn: sqlite3.Connection, group: Group, tz: ZoneInfo, now: int | None = None
) -> bool:
    """Allowed right now? One predicate with `group_status`, so they always agree."""
    return group_status(conn, group, tz, now)[0] in PERMITTED_STATUSES


def group_state(
    conn: sqlite3.Connection, group: Group, tz: ZoneInfo, now: int | None = None
) -> dict:
    """What the API reports about one group (the kid payload is a subset)."""
    now = int(now if now is not None else time.time())
    until = free_until(conn, group.id)
    scheduled = schedule_state(group, tz, now)
    open_now, change_ts = scheduled
    status, blocked_until = group_status(conn, group, tz, now, scheduled=scheduled)
    return {
        "id": group.id,
        "name": group.name,
        "emoji": group.emoji,
        "windows": [window.as_dict() for window in group.windows],
        "open_now": open_now,
        "next_change": change_ts,
        "opens_at": clock_str(change_ts, tz) if status == "outside_hours" else None,
        "open_until": (
            clock_str(change_ts, tz)
            if status == "allowed" and group.windows and change_ts is not None
            else None
        ),
        "status": status,
        "permitted": status in PERMITTED_STATUSES,
        "blocked_until": blocked_until,
        "free_until": until if until and until > now else None,
        "force_block_until": group.force_block_until if group.force_block_until > now else 0,
        "paused": group.paused,
        "owner_id": group.owner_id,
        "services": group.services,
        "domains": group.domains,
    }


def log_event(conn: sqlite3.Connection, kind: str, detail: dict | str = "") -> None:
    conn.execute(
        "INSERT INTO events(ts, kind, detail) VALUES(?,?,?)",
        (int(time.time()), kind, json.dumps(detail) if isinstance(detail, dict) else detail),
    )
    conn.commit()
