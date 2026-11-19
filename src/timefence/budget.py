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
CLOCK_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")

WINDOW_LIMIT = 4
WINDOW_DAYS = ("all", "weekday", "weekend")
BEDTIME_SCAN_STEPS = 1500  # a window is at most a day long
PERMITTED_STATUSES = frozenset({"allowed", "free"})


class ValidationError(ValueError):
    """User supplied something unusable."""


class WindowError(ValidationError):
    """A bedtime window cannot be used as given."""


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #


def day_str(ts: int, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")


def local_midnight(ts: int, tz: ZoneInfo) -> int:
    d = datetime.fromtimestamp(ts, tz).date()
    return int(datetime(d.year, d.month, d.day, tzinfo=tz).timestamp())


def next_local_midnight(ts: int, tz: ZoneInfo) -> int:
    """Start of the next local day; never `ts + 86400` (DST days are 23 or 25 hours)."""
    following = datetime.fromtimestamp(ts, tz).date() + timedelta(days=1)
    return int(datetime(following.year, following.month, following.day, tzinfo=tz).timestamp())


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
    force_block_until: int = 0  # admin "Block now" until midnight
    owner_id: int | None = None  # user this group belongs to (None = shared)
    services: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)

    def minutes_for(self, ts: int, tz: ZoneInfo) -> int:
        return self.weekend_minutes if is_weekend(ts, tz) else self.weekday_minutes


@dataclass(frozen=True)
class Window:
    """A daily block: minutes since local midnight, on days the window starts."""

    start_min: int
    end_min: int
    days: str = "all"

    def as_dict(self) -> dict[str, str]:
        return {
            "start": clock_text(self.start_min),
            "end": clock_text(self.end_min),
            "days": self.days,
        }


def normalize_windows(items: object) -> list[Window]:
    """Validate the wire format and return usable windows (at most four)."""
    if items is None:
        return []
    if not isinstance(items, list | tuple):
        raise WindowError("windows must be a list")
    if len(items) > WINDOW_LIMIT:
        raise WindowError(f"at most {WINDOW_LIMIT} bedtime windows")
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


def bedtime_state(
    conn: sqlite3.Connection, group: Group, tz: ZoneInfo, now: int | None = None
) -> tuple[bool, int | None]:
    """(a window is open now, the local end of the block).

    The end is the first whole minute at or after `now` that no window covers,
    found by stepping through minute boundaries. Building a local wall time for
    the end would silently pick a side on the days a clock skips or repeats.
    """
    now = now or int(time.time())
    if not group.windows or active_window(group, now, tz) is None:
        return False, None
    instant = now - (now % 60)
    for _ in range(BEDTIME_SCAN_STEPS):
        instant += 60
        if active_window(group, instant, tz) is None:
            return True, instant
    return True, None


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
        weekday_minutes=row["weekday_minutes"],
        weekend_minutes=row["weekend_minutes"],
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
    """Replace a group's bedtime windows; part of the group's own transaction."""
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
    weekday_minutes: int,
    weekend_minutes: int | None = None,
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
    bedtime = normalize_windows(windows)
    weekend = weekday_minutes if weekend_minutes is None else weekend_minutes
    for value in (weekday_minutes, weekend):
        if not (0 <= int(value) <= 1440):
            raise ValidationError("minutes must be between 0 and 1440")
    if not services and not domains:
        raise ValidationError("a group needs at least one service or domain")
    _assert_unique(conn, services, domains, None)
    cur = conn.execute(
        "INSERT INTO groups(name, emoji, weekday_minutes, weekend_minutes, paused, "
        "owner_id, created_at) VALUES(?,?,?,?,?,?,?)",
        (
            name,
            emoji.strip()[:8],
            int(weekday_minutes),
            int(weekend),
            int(paused),
            owner_id,
            now or int(time.time()),
        ),
    )
    gid = int(cur.lastrowid)
    _write_links(conn, gid, services, domains)
    _write_windows(conn, gid, bedtime)
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
    new_windows = group.windows if windows is None else normalize_windows(windows)

    conn.execute(
        "UPDATE groups SET name=?, emoji=?, weekday_minutes=?, weekend_minutes=?, "
        "paused=?, owner_id=? WHERE id=?",
        (
            new_name,
            (group.emoji if emoji is None else emoji.strip()[:8]),
            new_weekday,
            new_weekend,
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


def metered_seconds(conn: sqlite3.Connection, gid: int, day: str) -> int:
    row = conn.execute(
        "SELECT seconds FROM usage_seconds WHERE day=? AND group_id=?", (day, gid)
    ).fetchone()
    return int(row["seconds"]) if row else 0


def add_metered_seconds(
    conn: sqlite3.Connection, gid: int, day: str, seconds: int, cap_seconds: int | None = None
) -> int:
    """Add measured usage, never exceeding the cap and never lowering what is stored.

    A negative bonus can make the cap smaller than the usage already recorded;
    that must not erase real usage.
    """
    current = metered_seconds(conn, gid, day)
    if seconds <= 0:
        return current
    new = current + int(seconds)
    if cap_seconds is not None:
        new = min(new, int(cap_seconds))
    new = max(new, current)
    conn.execute(
        "INSERT INTO usage_seconds(day, group_id, seconds) VALUES(?,?,?) "
        "ON CONFLICT(day, group_id) DO UPDATE SET seconds=excluded.seconds",
        (day, gid, new),
    )
    conn.commit()
    return new


def used_seconds(conn: sqlite3.Connection, group: Group, day: str) -> int:
    """Seconds charged to `day`: only DNS activity ever counts as usage."""
    return metered_seconds(conn, group.id, day)


def budget_seconds(
    conn: sqlite3.Connection, group: Group, tz: ZoneInfo, now: int | None = None
) -> int:
    now = now or int(time.time())
    day = day_str(now, tz)
    return group.minutes_for(now, tz) * 60 + bonus_seconds(conn, group.id, day)


def cap_seconds(
    conn: sqlite3.Connection, group: Group, tz: ZoneInfo, now: int | None = None
) -> int:
    """The most usage that may be recorded today: the minutes plus positive bonus."""
    now = now or int(time.time())
    day = day_str(now, tz)
    return group.minutes_for(now, tz) * 60 + max(0, bonus_seconds(conn, group.id, day))


def remaining_seconds(
    conn: sqlite3.Connection, group: Group, tz: ZoneInfo, now: int | None = None
) -> int:
    now = now or int(time.time())
    day = day_str(now, tz)
    return max(0, budget_seconds(conn, group, tz, now) - used_seconds(conn, group, day))


def free_until(conn: sqlite3.Connection, gid: int) -> int | None:
    row = conn.execute("SELECT until_ts FROM free_allow WHERE group_id=?", (gid,)).fetchone()
    return int(row["until_ts"]) if row else None


def set_force_block(conn: sqlite3.Connection, gid: int, until_ts: int) -> None:
    conn.execute("UPDATE groups SET force_block_until=? WHERE id=?", (int(until_ts), gid))
    conn.commit()


def clear_force_block(conn: sqlite3.Connection, gid: int) -> None:
    conn.execute("UPDATE groups SET force_block_until=0 WHERE id=?", (gid,))
    conn.commit()


def is_permitted(
    conn: sqlite3.Connection, group: Group, tz: ZoneInfo, now: int | None = None
) -> bool:
    """Allowed right now? One predicate with `group_status`, so they always agree.

    A paused group is blocked. An admin "Free until" overrides everything,
    including a bedtime window (the admin asked for it). A force-block wins
    over the remaining budget and over bedtime; bedtime wins over the budget.
    """
    return group_status(conn, group, tz, now)[0] in PERMITTED_STATUSES


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


def reset_today(conn: sqlite3.Connection, gid: int, tz: ZoneInfo, now: int | None = None) -> None:
    """Forget today's metered usage, bonus and free-allow window for a group."""
    day = day_str(now or int(time.time()), tz)
    conn.execute("DELETE FROM usage_seconds WHERE day=? AND group_id=?", (day, gid))
    conn.execute("DELETE FROM bonus WHERE day=? AND group_id=?", (day, gid))
    conn.execute("DELETE FROM free_allow WHERE group_id=?", (gid,))
    conn.commit()


def history(
    conn: sqlite3.Connection,
    tz: ZoneInfo,
    days: int = 7,
    now: int | None = None,
    group_ids: set[int] | None = None,
) -> list[dict]:
    now = now or int(time.time())
    today = datetime.fromtimestamp(now, tz).date()
    groups = [g.id for g in list_groups(conn, include_deleted=True)]
    out = []
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        entry: dict[str, float | str] = {"day": day}
        for gid in groups:
            if group_ids is not None and gid not in group_ids:
                continue
            entry[str(gid)] = round(metered_seconds(conn, gid, day) / 60, 1)
        out.append(entry)
    return out


def usage_range(
    conn: sqlite3.Connection, tz: ZoneInfo, days: int = 7, now: int | None = None
) -> list[dict]:
    """Per group per local day: metered and bonus time, and the minutes allowed.

    Exactly two statements whatever the number of groups and days. Dates come
    from local date arithmetic (never 86400 offsets), and `budget_minutes` is the
    group's current weekday/weekend setting applied to that date, not history.
    """
    now = now or int(time.time())
    today = datetime.fromtimestamp(now, tz).date()
    dates = [(today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]
    first, last = dates[0], dates[-1]
    groups: dict[int, dict] = {}

    def collect(cursor: sqlite3.Cursor, bucket: str) -> None:
        for row in cursor:
            gid = int(row["id"])
            info = groups.setdefault(
                gid,
                {
                    "group_id": gid,
                    "group": str(row["name"]),
                    "weekday_minutes": int(row["weekday_minutes"]),
                    "weekend_minutes": int(row["weekend_minutes"]),
                    "used": {},
                    "bonus": {},
                },
            )
            if row["day"] is not None:
                info[bucket][str(row["day"])] = int(row["seconds"] or 0)

    collect(
        conn.execute(
            "SELECT g.id, g.name, g.weekday_minutes, g.weekend_minutes, u.day, u.seconds "
            "FROM groups g LEFT JOIN usage_seconds u "
            "ON u.group_id = g.id AND u.day BETWEEN ? AND ? "
            "WHERE g.deleted = 0 ORDER BY g.name, u.day",
            (first, last),
        ),
        "used",
    )
    collect(
        conn.execute(
            "SELECT g.id, g.name, g.weekday_minutes, g.weekend_minutes, b.day, b.seconds "
            "FROM groups g LEFT JOIN bonus b "
            "ON b.group_id = g.id AND b.day BETWEEN ? AND ? "
            "WHERE g.deleted = 0 ORDER BY g.name, b.day",
            (first, last),
        ),
        "bonus",
    )
    out: list[dict] = []
    for info in groups.values():
        for day in dates:
            used = info["used"].get(day, 0)
            bonus = info["bonus"].get(day, 0)
            weekend = date.fromisoformat(day).weekday() >= 5
            out.append(
                {
                    "group_id": info["group_id"],
                    "group": info["group"],
                    "day": day,
                    "used_seconds": used,
                    "bonus_seconds": bonus,
                    "used_minutes": round(used / 60, 1),
                    "bonus_minutes": round(bonus / 60, 1),
                    "budget_minutes": info["weekend_minutes"]
                    if weekend
                    else info["weekday_minutes"],
                }
            )
    return out


def log_event(conn: sqlite3.Connection, kind: str, detail: dict | str = "") -> None:
    conn.execute(
        "INSERT INTO events(ts, kind, detail) VALUES(?,?,?)",
        (int(time.time()), kind, json.dumps(detail) if isinstance(detail, dict) else detail),
    )
    conn.commit()


def group_status(
    conn: sqlite3.Connection,
    group: Group,
    tz: ZoneInfo,
    now: int | None = None,
    bedtime: tuple[bool, int | None] | None = None,
) -> tuple[str, int | None]:
    """One status per group, in the same order as `is_permitted`."""
    now = now or int(time.time())
    if group.paused or group.deleted:
        return "paused", None
    until = free_until(conn, group.id)
    if until and until > now:
        return "free", None
    if group.force_block_until and group.force_block_until > now:
        return "blocked_admin", group.force_block_until
    active, end_ts = bedtime if bedtime is not None else bedtime_state(conn, group, tz, now)
    if active:
        return "bedtime", end_ts
    if remaining_seconds(conn, group, tz, now) > 0:
        return "allowed", None
    return "limit_reached", next_local_midnight(now, tz)


def group_state(
    conn: sqlite3.Connection, group: Group, tz: ZoneInfo, now: int | None = None
) -> dict:
    now = now or int(time.time())
    day = day_str(now, tz)
    until = free_until(conn, group.id)
    bedtime = bedtime_state(conn, group, tz, now)
    active, end_ts = bedtime
    status, blocked_until = group_status(conn, group, tz, now, bedtime=bedtime)
    return {
        "id": group.id,
        "name": group.name,
        "emoji": group.emoji,
        "weekday_minutes": group.weekday_minutes,
        "weekend_minutes": group.weekend_minutes,
        "minutes_today": group.minutes_for(now, tz),
        "paused": group.paused,
        "force_block_until": group.force_block_until if group.force_block_until > now else 0,
        "owner_id": group.owner_id,
        "services": group.services,
        "domains": group.domains,
        "windows": [window.as_dict() for window in group.windows],
        "bonus_minutes": round(bonus_seconds(conn, group.id, day) / 60, 1),
        "used_minutes": round(used_seconds(conn, group, day) / 60, 1),
        "remaining_minutes": round(remaining_seconds(conn, group, tz, now) / 60, 1),
        "permitted": status in PERMITTED_STATUSES,
        "status": status,
        "blocked_until": blocked_until,
        "free_until": until if until and until > now else None,
        "bedtime_active": active,
        "bedtime_until": clock_str(end_ts, tz) if end_ts is not None else None,
    }


def today(tz: ZoneInfo, now: int | None = None) -> date:
    return datetime.fromtimestamp(now or int(time.time()), tz).date()
