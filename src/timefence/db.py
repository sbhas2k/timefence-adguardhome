"""SQLite storage with simple versioned migrations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

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

MIGRATIONS: dict[int, str] = {1: MIGRATION_1, 2: MIGRATION_2, 3: MIGRATION_3}


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:  # the database holds password hashes; keep it private
        path.chmod(0o600)
    except OSError:
        pass
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every migration the database is missing. Returns the new version."""
    version = current_version(conn)
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{version} is newer than this build (v{SCHEMA_VERSION})"
        )
    for target in sorted(MIGRATIONS):
        if target <= version:
            continue
        conn.executescript(MIGRATIONS[target])
        conn.execute(f"PRAGMA user_version={target}")
        conn.commit()
        version = target
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
