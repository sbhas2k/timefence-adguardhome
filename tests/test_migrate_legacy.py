"""The legacy importer must write user accounts and daily usage, safely."""

from __future__ import annotations

import runpy
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from timefence import db, security

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "migrate_legacy.py"
TZ = ZoneInfo("Europe/London")


def legacy_db(path: Path, admin_hash: str, *, sessions: list[tuple] | None = None) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE site_settings (site TEXT PRIMARY KEY, daily_minutes INTEGER);
        CREATE TABLE credentials (role TEXT PRIMARY KEY, hash TEXT, updated_at INTEGER);
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, site TEXT, planned_seconds INTEGER,
            started_at INTEGER, ended_at INTEGER, used_seconds INTEGER,
            end_reason TEXT, actor TEXT
        );
        CREATE TABLE bonus (day TEXT, site TEXT, seconds INTEGER);
        CREATE TABLE free_allow (site TEXT PRIMARY KEY, until_ts INTEGER);
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, detail TEXT
        );
        """
    )
    conn.execute("INSERT INTO site_settings(site, daily_minutes) VALUES('youtube', 30)")
    conn.execute(
        "INSERT INTO credentials(role, hash, updated_at) VALUES('admin', ?, 0)", (admin_hash,)
    )
    for row in sessions or []:
        conn.execute(
            "INSERT INTO sessions(site, planned_seconds, started_at, ended_at, used_seconds, "
            "end_reason, actor) VALUES(?,?,?,?,?,?,?)",
            row,
        )
    conn.commit()
    conn.close()


def run_import(legacy: Path, target: Path, tz: str = "Europe/London") -> int:
    module = runpy.run_path(str(SCRIPT), run_name="not_main")
    return module["main"](["--legacy", str(legacy), "--target", str(target), "--tz", tz])


def test_legacy_import_never_leaves_default_admin(tmp_path):
    legacy = tmp_path / "legacy.db"
    target = tmp_path / "timefence.db"
    legacy_db(legacy, security.hash_secret("legacy-password"))

    assert run_import(legacy, target) == 0

    conn = db.connect(target)
    assert db.get_bool_setting(conn, "setup_complete") is True
    row = conn.execute("SELECT hash FROM users WHERE username='admin'").fetchone()
    assert security.verify_secret(row["hash"], "legacy-password")
    assert not security.verify_secret(row["hash"], "admin")
    conn.close()


def test_legacy_default_hash_leaves_setup_open(tmp_path):
    legacy = tmp_path / "legacy.db"
    target = tmp_path / "timefence.db"
    legacy_db(legacy, security.hash_secret("admin"))

    assert run_import(legacy, target) == 0

    conn = db.connect(target)
    assert db.get_bool_setting(conn, "setup_complete") is False
    conn.close()


def test_legacy_sessions_become_usage(tmp_path):
    legacy = tmp_path / "legacy.db"
    target = tmp_path / "timefence.db"
    started = int(datetime(2026, 11, 12, 21, 0, tzinfo=TZ).timestamp())
    legacy_db(
        legacy,
        security.hash_secret("legacy-password"),
        sessions=[("youtube", 900, started, started + 300, 300, "stopped", "admin")],
    )

    assert run_import(legacy, target) == 0

    conn = db.connect(target)
    group = conn.execute("SELECT id FROM groups WHERE name='YouTube'").fetchone()
    rows = {
        row["day"]: row["seconds"]
        for row in conn.execute(
            "SELECT day, seconds FROM usage_seconds WHERE group_id=?", (group["id"],)
        )
    }
    assert rows == {"2026-11-12": 300}
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    conn.close()
