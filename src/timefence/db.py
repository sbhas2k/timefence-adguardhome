"""SQLite storage with versioned, atomic migrations."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("timefence.db")

SCHEMA_VERSION = 7
SKIP_BACKUP_ENV = "TIMEFENCE_SKIP_MIGRATION_BACKUP"

MIGRATION_1 = """
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE credentials (
    role       TEXT PRIMARY KEY,          -- 'admin' | 'user'
    hash       TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    emoji           TEXT NOT NULL DEFAULT '',
    weekday_minutes INTEGER NOT NULL,
    weekend_minutes INTEGER NOT NULL,
    paused          INTEGER NOT NULL DEFAULT 0,
    deleted         INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL
);
CREATE UNIQUE INDEX groups_name_unique ON groups(name) WHERE deleted = 0;

CREATE TABLE group_services (
    group_id   INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    service_id TEXT NOT NULL,
    PRIMARY KEY (group_id, service_id)
);
CREATE UNIQUE INDEX group_services_unique ON group_services(service_id);

CREATE TABLE owned_services (
    service_id TEXT PRIMARY KEY,
    added_at   INTEGER NOT NULL
);

CREATE TABLE group_domains (
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    domain   TEXT NOT NULL,
    PRIMARY KEY (group_id, domain)
);
CREATE UNIQUE INDEX group_domains_unique ON group_domains(domain);

CREATE TABLE sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        INTEGER NOT NULL REFERENCES groups(id),
    planned_seconds INTEGER NOT NULL,
    started_at      INTEGER NOT NULL,
    ended_at        INTEGER,
    used_seconds    INTEGER NOT NULL DEFAULT 0,
    end_reason      TEXT,
    actor           TEXT
);
CREATE INDEX sessions_group_started ON sessions(group_id, started_at);

CREATE TABLE bonus (
    day      TEXT NOT NULL,               -- YYYY-MM-DD in local TZ
    group_id INTEGER NOT NULL REFERENCES groups(id),
    seconds  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, group_id)
);

CREATE TABLE free_allow (
    group_id INTEGER PRIMARY KEY REFERENCES groups(id) ON DELETE CASCADE,
    until_ts INTEGER NOT NULL
);

CREATE TABLE events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     INTEGER NOT NULL,
    kind   TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX events_ts ON events(ts);
"""

# v2: multi-user accounts, "allowed until the budget runs out" groups and
#     query-log usage metering.
MIGRATION_2 = """
CREATE TABLE users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role        TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    hash        TEXT NOT NULL,
    disabled    INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    last_login  INTEGER
);

ALTER TABLE groups ADD COLUMN mode TEXT NOT NULL DEFAULT 'auto';
ALTER TABLE groups ADD COLUMN force_block_until INTEGER NOT NULL DEFAULT 0;

CREATE TABLE usage_seconds (
    day      TEXT NOT NULL,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    seconds  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, group_id)
);

INSERT OR IGNORE INTO users(username, role, hash, created_at, updated_at)
    SELECT 'admin', 'admin', hash, updated_at, updated_at
    FROM credentials WHERE role = 'admin';
INSERT OR IGNORE INTO users(username, role, hash, created_at, updated_at)
    SELECT 'user', 'user', hash, updated_at, updated_at
    FROM credentials WHERE role = 'user';
"""

# v3: per-account budgets - a group can be owned by a user.
MIGRATION_3 = """
ALTER TABLE groups ADD COLUMN owner_id INTEGER REFERENCES users(id);
"""

# v5: bedtime windows per group and the alert de-duplication record. Pure DDL,
# so a CLI command can still open and upgrade an older database without
# starting the server.
MIGRATION_5 = """
CREATE TABLE group_windows (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id  INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    start_min INTEGER NOT NULL CHECK (start_min BETWEEN 0 AND 1439),
    end_min   INTEGER NOT NULL CHECK (end_min BETWEEN 0 AND 1439),
    days      TEXT NOT NULL CHECK (days IN ('all', 'weekday', 'weekend')),
    CHECK (start_min <> end_min)
);
CREATE INDEX group_windows_group ON group_windows(group_id);

CREATE TABLE alerts_sent (
    key     TEXT PRIMARY KEY,
    sent_at INTEGER NOT NULL
);
CREATE INDEX alerts_sent_ts ON alerts_sent(sent_at);
"""


# v6: the daily minutes budget, bonus time and DNS usage metering are gone.
# Allowed windows are the only schedule, so the stored block windows (whose
# meaning would silently invert) are cleared. Pure DDL, so a CLI command can
# still open and upgrade an older database without starting the server.
MIGRATION_6 = """
DROP TABLE IF EXISTS bonus;
DROP TABLE IF EXISTS usage_seconds;
DELETE FROM group_windows;
ALTER TABLE groups DROP COLUMN weekday_minutes;
ALTER TABLE groups DROP COLUMN weekend_minutes;
"""


# v7: a window's days are `all` or a comma-separated list of day names
# (`mon,fri`) instead of only the weekday/weekend buckets. SQLite cannot change
# a CHECK constraint in place, so the table is recreated and its rows mapped:
# all -> all, weekday -> mon..fri, weekend -> sat,sun. A callable step, like v4,
# because it rewrites data. A fresh database runs steps 5, 6 and 7 in order, so
# step 7 drops the v5 table it has just created and replaces it with this one;
# the runner applies each step once, so that is safe.
MIGRATION_7_CREATE = """
CREATE TABLE group_windows_v7 (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id  INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    start_min INTEGER NOT NULL CHECK (start_min BETWEEN 0 AND 1439),
    end_min   INTEGER NOT NULL CHECK (end_min BETWEEN 0 AND 1439),
    days      TEXT NOT NULL,
    CHECK (start_min <> end_min)
)
"""

MIGRATION_7_COPY = """
INSERT INTO group_windows_v7(id, group_id, start_min, end_min, days)
    SELECT id, group_id, start_min, end_min,
        CASE days
            WHEN 'weekday' THEN 'mon,tue,wed,thu,fri'
            WHEN 'weekend' THEN 'sat,sun'
            ELSE days
        END
    FROM group_windows
"""

MIGRATION_7_DROP = "DROP TABLE group_windows"
MIGRATION_7_RENAME = "ALTER TABLE group_windows_v7 RENAME TO group_windows"
MIGRATION_7_INDEX = "CREATE INDEX group_windows_group ON group_windows(group_id)"


def _migration_7(conn: sqlite3.Connection, tz: ZoneInfo) -> None:
    """Map every stored window's day filter to the v7 day list."""
    # Every statement stays inside the runner's transaction; executescript would
    # commit the caller's work before running, which is not atomic.
    conn.execute(MIGRATION_7_CREATE)
    conn.execute(MIGRATION_7_COPY)
    conn.execute(MIGRATION_7_DROP)
    conn.execute(MIGRATION_7_RENAME)
    conn.execute(MIGRATION_7_INDEX)


class DataMigrationRequired(RuntimeError):
    """A pending migration rewrites data and the caller may not run it."""


ACCESS_ERROR = "database access without the store lock"


class StoreLock:
    """A re-entrant lock that remembers which thread is holding it."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._owner: int | None = None
        self._depth = 0

    def acquire(self) -> bool:
        self._lock.acquire()
        self._owner = threading.get_ident()
        self._depth += 1
        return True

    def release(self) -> None:
        self._depth -= 1
        if self._depth <= 0:
            self._depth = 0
            self._owner = None
        self._lock.release()

    def __enter__(self) -> StoreLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.release()
        return False

    def held_by_current_thread(self) -> bool:
        return self._owner == threading.get_ident()


class GuardedCursor(sqlite3.Cursor):
    """A cursor that refuses to run or iterated without the store lock."""

    def _check(self) -> None:
        lock = getattr(self.connection, "tf_lock", None)
        if lock is not None and not lock.held_by_current_thread():
            raise RuntimeError(ACCESS_ERROR)

    def execute(self, sql: str, parameters: Any = (), /) -> GuardedCursor:
        self._check()
        return super().execute(sql, parameters)

    def executemany(self, sql: str, parameters: Any, /) -> GuardedCursor:
        self._check()
        return super().executemany(sql, parameters)

    def executescript(self, sql: str, /) -> GuardedCursor:
        self._check()
        return super().executescript(sql)

    def fetchone(self) -> Any:
        self._check()
        return super().fetchone()

    def fetchmany(self, size: int | None = None) -> list[Any]:
        self._check()
        return super().fetchmany(size) if size is not None else super().fetchmany()

    def fetchall(self) -> list[Any]:
        self._check()
        return super().fetchall()

    def __next__(self) -> Any:
        self._check()
        return super().__next__()


class GuardedConnection(sqlite3.Connection):
    """SQLite connection that only works while its store lock is held."""

    tf_lock: StoreLock | None = None

    def _check(self) -> None:
        if self.tf_lock is not None and not self.tf_lock.held_by_current_thread():
            raise RuntimeError(ACCESS_ERROR)

    def cursor(self, factory: Any = GuardedCursor) -> GuardedCursor:
        self._check()
        return super().cursor(factory)

    def execute(self, sql: str, parameters: Any = (), /) -> GuardedCursor:
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, parameters: Any, /) -> GuardedCursor:
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql: str, /) -> GuardedCursor:
        self._check()
        return super().executescript(sql)

    def commit(self) -> None:
        self._check()
        super().commit()

    def rollback(self) -> None:
        self._check()
        super().rollback()

    def backup(self, target: Any, **kwargs: Any) -> None:
        self._check()
        super().backup(target, **kwargs)

    def __enter__(self) -> GuardedConnection:
        self._check()
        return super().__enter__()

    def __exit__(self, *exc_info: object) -> bool:
        self._check()
        return bool(super().__exit__(*exc_info))


class Store:
    """One connection behind one lock: every database access goes through it."""

    def __init__(self, path: Path | str) -> None:
        self.lock = StoreLock()
        self.conn = connect(path, lock=self.lock)

    def __enter__(self) -> GuardedConnection:
        self.lock.acquire()
        return self.conn

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        try:
            if exc_type is not None and self.conn.in_transaction:
                self.conn.rollback()
        finally:
            self.lock.release()
        return False

    def close(self) -> None:
        with self as conn:
            conn.close()


def _default_zone() -> ZoneInfo:
    name = os.environ.get("TZ") or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _clock() -> int:
    """The current time for the v4 data migration (a seam for its test)."""
    return int(time.time())


def _close_open_sessions(conn: sqlite3.Connection, now: int) -> int:
    """Bill elapsed time for sessions a previous run left open."""
    closed = 0
    rows = conn.execute(
        "SELECT id, started_at, planned_seconds FROM sessions WHERE ended_at IS NULL"
    ).fetchall()
    for row in rows:
        used = max(0, min(int(row["planned_seconds"]), now - int(row["started_at"])))
        conn.execute(
            "UPDATE sessions SET ended_at=?, used_seconds=?, end_reason='migrated' WHERE id=?",
            (int(row["started_at"]) + used, used, row["id"]),
        )
        closed += 1
    return closed


def _backfill_session_usage(conn: sqlite3.Connection, tz: ZoneInfo) -> int:
    """Carry session billing into daily usage for the groups that used sessions."""
    session_groups = {
        int(row["id"]) for row in conn.execute("SELECT id FROM groups WHERE mode = 'session'")
    }
    totals: dict[tuple[str, int], int] = {}
    for row in conn.execute("SELECT group_id, started_at, used_seconds FROM sessions"):
        gid = int(row["group_id"])
        if gid not in session_groups:
            continue
        day = datetime.fromtimestamp(int(row["started_at"]), tz).strftime("%Y-%m-%d")
        key = (day, gid)
        totals[key] = totals.get(key, 0) + int(row["used_seconds"])
    for (day, gid), seconds in totals.items():
        conn.execute(
            "INSERT INTO usage_seconds(day, group_id, seconds) VALUES(?,?,?) "
            "ON CONFLICT(day, group_id) DO UPDATE "
            "SET seconds = usage_seconds.seconds + excluded.seconds",
            (day, gid, seconds),
        )
    return len(totals)


def _migration_4(conn: sqlite3.Connection, tz: ZoneInfo) -> None:
    """Session mode is gone: convert groups, keep their usage, add the epoch."""
    now = _clock()
    closed = _close_open_sessions(conn, now)
    backfilled = _backfill_session_usage(conn, tz)
    converted = conn.execute("UPDATE groups SET mode = 'auto' WHERE mode <> 'auto'").rowcount
    conn.execute("ALTER TABLE users ADD COLUMN session_epoch INTEGER NOT NULL DEFAULT 0")
    detail = json.dumps(
        {
            "to": 4,
            "closed_sessions": closed,
            "backfilled_rows": backfilled,
            "converted_groups": converted,
        }
    )
    conn.execute("INSERT INTO events(ts, kind, detail) VALUES(?,?,?)", (now, "migration", detail))


MIGRATIONS: dict[int, str | Callable[[sqlite3.Connection, ZoneInfo], None]] = {
    1: MIGRATION_1,
    2: MIGRATION_2,
    3: MIGRATION_3,
    4: _migration_4,
    5: MIGRATION_5,
    6: MIGRATION_6,
    7: _migration_7,
}


def _main_path(conn: sqlite3.Connection) -> str:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return str(row[2] or "")
    return ""


def backup_path(source: Path, goal: int) -> Path:
    """Where the pre-upgrade copy of `source` for schema v`goal` lives."""
    return source.with_name(f"{source.name}.pre-v{goal}.bak")


def _backup_before(conn: sqlite3.Connection, goal: int, version: int) -> None:
    """Write one verified copy of the database next to it, before any change."""
    source = _main_path(conn)
    if not source or source == ":memory:":
        return
    if os.environ.get(SKIP_BACKUP_ENV) == "1":
        log.warning("pre-upgrade backup skipped because %s=1", SKIP_BACKUP_ENV)
        return
    dest = backup_path(Path(source), goal)
    temporary = Path(str(dest) + ".tmp")
    conn.commit()  # the copy must not race the caller's pending writes
    try:
        temporary.unlink(missing_ok=True)
        with contextlib.closing(sqlite3.connect(temporary)) as copy:
            conn.backup(copy)
        with contextlib.closing(sqlite3.connect(temporary)) as check:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("the copy failed its integrity check")
            copied = int(check.execute("PRAGMA user_version").fetchone()[0])
            if copied != version:
                raise RuntimeError(f"the copy reports schema v{copied}, expected v{version}")
        with contextlib.suppress(OSError):
            os.chmod(temporary, 0o600)
        os.replace(temporary, dest)
    except Exception as exc:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise RuntimeError(
            f"cannot write the pre-upgrade backup {dest}: {exc}; "
            f"fix the cause or set {SKIP_BACKUP_ENV}=1"
        ) from exc


def _apply_script(conn: sqlite3.Connection, sql: str, target: int) -> None:
    conn.commit()
    try:
        conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version={target};\nCOMMIT;")
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _apply_callable(
    conn: sqlite3.Connection,
    step: Callable[[sqlite3.Connection, ZoneInfo], None],
    target: int,
    tz: ZoneInfo,
) -> None:
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if current_version(conn) >= target:
            conn.rollback()
            return
        step(conn, tz)
        conn.execute(f"PRAGMA user_version={target}")
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def connect(path: Path | str, lock: StoreLock | None = None) -> GuardedConnection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False, factory=GuardedConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:  # the database holds password hashes; keep it private
        path.chmod(0o600)
    except OSError:
        pass
    conn.tf_lock = lock
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(
    conn: sqlite3.Connection,
    tz: ZoneInfo | None = None,
    target: int | None = None,
    allow_data_migration: bool = True,
) -> int:
    """Apply every migration the database is missing. Returns the new version."""
    version = current_version(conn)
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{version} is newer than this build (v{SCHEMA_VERSION})"
        )
    tz = tz or _default_zone()
    goal = SCHEMA_VERSION if target is None else int(target)
    initial = version
    pending = [step for step in sorted(MIGRATIONS) if version < step <= goal]
    if not allow_data_migration and any(callable(MIGRATIONS[step]) for step in pending):
        raise DataMigrationRequired(
            f"database is at schema v{version}; start TimeFence once to upgrade it, "
            "then run this command again"
        )
    backed_up = False
    for step in pending:
        if not backed_up and 1 <= initial < step:
            _backup_before(conn, pending[-1], initial)
            backed_up = True
        migration = MIGRATIONS[step]
        if callable(migration):
            _apply_callable(conn, migration, step, tz)
        else:
            _apply_script(conn, migration, step)
        version = step
    return version


# --------------------------------------------------------------------------- #
# settings helpers (key/value)
# --------------------------------------------------------------------------- #


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def get_bool_setting(conn: sqlite3.Connection, key: str, default: bool = False) -> bool:
    value = get_setting(conn, key)
    if value is None:
        return default
    return value == "1"


def set_bool_setting(conn: sqlite3.Connection, key: str, value: bool) -> None:
    set_setting(conn, key, "1" if value else "0")


def add_event(conn: sqlite3.Connection, kind: str, detail: str = "") -> None:
    import time

    conn.execute(
        "INSERT INTO events(ts, kind, detail) VALUES(?,?,?)", (int(time.time()), kind, detail)
    )
    conn.commit()
