"""Schema migrations: atomic steps, the session-mode removal and the backup."""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from timefence import db

TZ = ZoneInfo("Europe/London")


def build_v3_db(path) -> sqlite3.Connection:
    conn = db.connect(path)
    assert db.migrate(conn, TZ, target=3) == 3
    return conn


def add_group(conn, name, *, mode="session", deleted=0, created_at=0) -> int:
    cur = conn.execute(
        "INSERT INTO groups(name, emoji, weekday_minutes, weekend_minutes, paused, deleted, "
        "created_at, mode) VALUES(?,?,?,?,0,?,?,?)",
        (name, "", 60, 60, deleted, created_at, mode),
    )
    return int(cur.lastrowid)


def add_session(conn, group_id, started_at, used_seconds, planned=900, ended=True):
    conn.execute(
        "INSERT INTO sessions(group_id, planned_seconds, started_at, ended_at, used_seconds, "
        "end_reason) VALUES(?,?,?,?,?,?)",
        (
            group_id,
            planned,
            started_at,
            started_at + used_seconds if ended else None,
            used_seconds,
            "stopped" if ended else None,
        ),
    )


def local_ts(day: str, hour: int = 12) -> int:
    year, month, dom = (int(part) for part in day.split("-"))
    return int(datetime(year, month, dom, hour, tzinfo=TZ).timestamp())


def usage(conn, group_id, day) -> int:
    row = conn.execute(
        "SELECT seconds FROM usage_seconds WHERE day=? AND group_id=?", (day, group_id)
    ).fetchone()
    return int(row[0]) if row else 0


def test_v3_session_groups_become_auto_with_usage_backfilled(tmp_path):
    conn = build_v3_db(tmp_path / "t.db")
    d1, d2 = "2026-11-10", "2026-11-11"
    session_group = add_group(conn, "Sessions")
    auto_group = add_group(conn, "Auto", mode="auto")
    deleted_group = add_group(conn, "Gone", deleted=1)
    add_session(conn, session_group, local_ts(d1), 300)
    add_session(conn, session_group, local_ts(d1) + 400, 600)
    add_session(conn, session_group, local_ts(d2), 120)
    add_session(conn, deleted_group, local_ts(d1), 60)
    conn.execute(
        "INSERT INTO usage_seconds(day, group_id, seconds) VALUES(?,?,?)", (d1, auto_group, 500)
    )
    conn.commit()

    assert db.migrate(conn, TZ) == 5

    assert db.current_version(conn) == 5
    assert {row[0] for row in conn.execute("SELECT DISTINCT mode FROM groups")} == {"auto"}
    assert usage(conn, session_group, d1) == 900
    assert usage(conn, session_group, d2) == 120
    assert usage(conn, auto_group, d1) == 500
    assert usage(conn, deleted_group, d1) == 60
    conn.close()


def test_open_sessions_are_closed_and_billed_elapsed(tmp_path):
    conn = build_v3_db(tmp_path / "t.db")
    group = add_group(conn, "Open")
    now = int(time.time())
    add_session(conn, group, now - 600, 0, planned=900, ended=False)
    add_session(conn, group, now - 3600, 0, planned=900, ended=False)

    db.migrate(conn, TZ)

    rows = list(
        conn.execute(
            "SELECT started_at, ended_at, used_seconds, end_reason FROM sessions ORDER BY id"
        )
    )
    assert rows[0]["used_seconds"] == 600
    assert rows[0]["ended_at"] == rows[0]["started_at"] + 600
    assert rows[1]["used_seconds"] == 900
    assert all(row["end_reason"] == "migrated" for row in rows)
    day = datetime.fromtimestamp(rows[0]["started_at"], TZ).strftime("%Y-%m-%d")
    assert usage(conn, group, day) == 1500
    conn.close()


def test_session_started_before_midnight_is_billed_to_its_start_day(tmp_path):
    conn = build_v3_db(tmp_path / "t.db")
    group = add_group(conn, "Late")
    late = local_ts("2026-11-10", hour=23) + 58 * 60
    add_session(conn, group, late, 300)

    db.migrate(conn, TZ)

    assert usage(conn, group, "2026-11-10") == 300
    assert usage(conn, group, "2026-11-11") == 0
    conn.close()


def test_v4_adds_session_epoch_and_logs_event(tmp_path):
    conn = build_v3_db(tmp_path / "t.db")
    group = add_group(conn, "Sessions")
    add_session(conn, group, local_ts("2026-11-10"), 300)
    conn.execute(
        "INSERT INTO users(username, role, hash, created_at, updated_at) "
        "VALUES('admin','admin','x',0,0)"
    )
    conn.commit()

    db.migrate(conn, TZ)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    assert "session_epoch" in columns
    assert conn.execute("SELECT session_epoch FROM users").fetchone()[0] == 0
    events = list(conn.execute("SELECT ts, kind, detail FROM events WHERE kind='migration'"))
    assert len(events) == 1
    detail = events[0]["detail"]
    assert '"to": 4' in detail
    assert '"closed_sessions": 0' in detail
    assert '"backfilled_rows": 1' in detail
    assert '"converted_groups": 1' in detail
    conn.close()


def test_backup_is_written_before_v4(tmp_path):
    path = tmp_path / "t.db"
    conn = build_v3_db(path)
    add_group(conn, "Sessions")
    conn.commit()
    backup = path.with_name(path.name + ".pre-v4.bak")
    temporary = path.with_name(path.name + ".pre-v4.bak.tmp")
    backup.write_bytes(b"corrupt, not a database")
    temporary.write_bytes(b"stale leftovers")

    # a migration that stops at v4 names its copy after v4
    db.migrate(conn, TZ, target=4)

    assert backup.exists()
    assert not temporary.exists()
    with sqlite3.connect(backup) as check:
        assert int(check.execute("PRAGMA user_version").fetchone()[0]) == 3
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    if os.name != "nt":
        assert backup.stat().st_mode & 0o777 == 0o600
    conn.close()


def test_migration_aborts_when_backup_fails(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    conn = build_v3_db(path)
    add_group(conn, "Sessions")
    conn.commit()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(db.os, "replace", boom)

    with pytest.raises(RuntimeError) as excinfo:
        db.migrate(conn, TZ, target=4)

    assert "pre-v4.bak" in str(excinfo.value)
    assert "TIMEFENCE_SKIP_MIGRATION_BACKUP=1" in str(excinfo.value)
    assert db.current_version(conn) == 3
    assert not path.with_name(path.name + ".pre-v4.bak.tmp").exists()
    assert not path.with_name(path.name + ".pre-v4.bak").exists()
    conn.close()


def test_failed_v4_leaves_v3_intact(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    conn = build_v3_db(path)
    group = add_group(conn, "Sessions")
    add_session(conn, group, local_ts("2026-11-10"), 300)
    add_session(conn, group, int(time.time()) - 600, 0, planned=900, ended=False)
    conn.commit()

    def failing(connection, tz):
        db._close_open_sessions(connection, int(time.time()))
        db._backfill_session_usage(connection, tz)
        raise RuntimeError("boom")

    monkeypatch.setitem(db.MIGRATIONS, 4, failing)

    with pytest.raises(RuntimeError, match="boom"):
        db.migrate(conn, TZ)

    assert db.current_version(conn) == 3
    assert {row[0] for row in conn.execute("SELECT mode FROM groups")} == {"session"}
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM usage_seconds").fetchone()[0] == 0
    monkeypatch.undo()

    assert db.migrate(conn, TZ) == 5
    assert usage(conn, group, "2026-11-10") == 300
    conn.close()


def test_failed_string_migration_is_atomic(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "t.db")
    assert db.migrate(conn, TZ, target=2) == 2
    add_group(conn, "Sessions")
    conn.commit()
    monkeypatch.setitem(
        db.MIGRATIONS,
        3,
        "ALTER TABLE groups ADD COLUMN owner_id INTEGER;\nINSERT INTO no_such_table VALUES(1);",
    )

    with pytest.raises(sqlite3.Error):
        db.migrate(conn, TZ, target=3)

    assert db.current_version(conn) == 2
    assert "owner_id" not in {row[1] for row in conn.execute("PRAGMA table_info(groups)")}
    conn.close()


def test_fresh_db_writes_no_backup(tmp_path):
    path = tmp_path / "fresh.db"
    conn = db.connect(path)

    assert db.migrate(conn, TZ) == 5
    assert not path.with_name(path.name + ".pre-v4.bak").exists()
    assert not path.with_name(path.name + ".pre-v5.bak").exists()
    conn.close()


def test_newer_db_is_refused(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("PRAGMA user_version=6")
    conn.commit()

    with pytest.raises(RuntimeError, match="newer than this build"):
        db.migrate(conn, TZ)
    assert db.current_version(conn) == 6
    conn.close()


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "t.db"
    conn = build_v4_db(path)
    normal_group(conn)
    conn.commit()

    assert db.migrate(conn, TZ) == 5
    backup = path.with_name(path.name + ".pre-v5.bak")
    assert backup.exists()
    stamp = backup.stat().st_mtime_ns

    assert db.migrate(conn, TZ) == 5
    # a second call applies nothing, so it must not rewrite the copy
    assert backup.stat().st_mtime_ns == stamp
    assert not path.with_name(path.name + ".pre-v4.bak").exists()
    conn.close()


def test_migrate_refuses_data_migration_when_not_allowed(tmp_path):
    path = tmp_path / "t.db"
    conn = build_v3_db(path)
    group = add_group(conn, "Sessions")
    add_session(conn, group, local_ts("2026-11-10"), 300)
    conn.commit()

    with pytest.raises(db.DataMigrationRequired) as excinfo:
        db.migrate(conn, TZ, allow_data_migration=False)

    assert "start TimeFence once" in str(excinfo.value)
    assert db.current_version(conn) == 3
    assert {row[0] for row in conn.execute("SELECT mode FROM groups")} == {"session"}
    conn.close()


def test_passwd_works_on_v4_db(tmp_path):
    """A pure-DDL v5 step still lets the CLI open and upgrade an older database."""
    path = tmp_path / "t.db"
    conn = build_v4_db(path)
    normal_group(conn)
    conn.commit()

    assert db.migrate(conn, TZ, allow_data_migration=False) == 5
    assert db.current_version(conn) == 5
    assert path.with_name(path.name + ".pre-v5.bak").exists()
    conn.close()


# --------------------------------------------------------------------------- #
# schema v5: bedtime windows and alert de-duplication
# --------------------------------------------------------------------------- #


def build_v4_db(path) -> sqlite3.Connection:
    conn = db.connect(path)
    assert db.migrate(conn, TZ, target=4) == 4
    return conn


def normal_group(conn, name="YouTube") -> int:
    cur = conn.execute(
        "INSERT INTO groups(name, emoji, weekday_minutes, weekend_minutes, paused, deleted, "
        "created_at) VALUES(?,?,?,?,0,0,0)",
        (name, "", 60, 60),
    )
    return int(cur.lastrowid)


def test_v5_creates_the_window_and_alert_tables(tmp_path):
    conn = build_v4_db(tmp_path / "t.db")

    assert db.migrate(conn, TZ) == 5
    assert db.current_version(conn) == 5

    window_columns = {row[1] for row in conn.execute("PRAGMA table_info(group_windows)")}
    assert window_columns == {"id", "group_id", "start_min", "end_min", "days"}
    alert_columns = {row[1] for row in conn.execute("PRAGMA table_info(alerts_sent)")}
    assert alert_columns == {"key", "sent_at"}

    # a window must reference a real group and use a known days value
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO group_windows(group_id, start_min, end_min, days) VALUES(?,?,?,?)",
            (12345, 1260, 420, "all"),
        )
    conn.rollback()
    group = normal_group(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO group_windows(group_id, start_min, end_min, days) VALUES(?,?,?,?)",
            (group, 1260, 420, "daily"),
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO group_windows(group_id, start_min, end_min, days) VALUES(?,?,?,?)",
            (group, 1260, 1260, "all"),
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO group_windows(group_id, start_min, end_min, days) VALUES(?,?,?,?)",
            (group, 1440, 420, "all"),
        )
    conn.rollback()
    conn.close()


def test_v5_keeps_existing_rows(tmp_path):
    conn = build_v4_db(tmp_path / "t.db")
    group = normal_group(conn, "Games")
    conn.execute(
        "INSERT INTO usage_seconds(day, group_id, seconds) VALUES('2026-11-10', ?, 600)", (group,)
    )
    conn.execute(
        "INSERT INTO users(username, role, hash, created_at, updated_at) "
        "VALUES('admin','admin','x',0,0)"
    )
    conn.commit()

    assert db.migrate(conn, TZ) == 5

    assert conn.execute("SELECT name FROM groups").fetchone()[0] == "Games"
    assert usage(conn, group, "2026-11-10") == 600
    assert conn.execute("SELECT username FROM users").fetchone()[0] == "admin"
    assert conn.execute("SELECT COUNT(*) FROM group_windows").fetchone()[0] == 0
    conn.close()


def test_v4_to_v5_writes_pre_v5_backup(tmp_path):
    path = tmp_path / "t.db"
    conn = build_v4_db(path)
    normal_group(conn)
    conn.commit()
    backup = path.with_name(path.name + ".pre-v5.bak")
    backup.write_bytes(b"corrupt, not a database")

    db.migrate(conn, TZ)

    with sqlite3.connect(backup) as check:
        assert int(check.execute("PRAGMA user_version").fetchone()[0]) == 4
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert check.execute("SELECT name FROM groups").fetchone()[0] == "YouTube"
    if os.name != "nt":
        assert backup.stat().st_mode & 0o777 == 0o600
    conn.close()


def test_migration_aborts_when_the_v5_backup_fails(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    conn = build_v4_db(path)
    normal_group(conn)
    conn.commit()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(db.os, "replace", boom)

    with pytest.raises(RuntimeError) as excinfo:
        db.migrate(conn, TZ)

    assert "pre-v5.bak" in str(excinfo.value)
    assert "TIMEFENCE_SKIP_MIGRATION_BACKUP=1" in str(excinfo.value)
    assert db.current_version(conn) == 4
    assert not path.with_name(path.name + ".pre-v5.bak.tmp").exists()
    assert not path.with_name(path.name + ".pre-v5.bak").exists()
    conn.close()


def test_failed_v5_leaves_v4_intact(tmp_path, monkeypatch):
    conn = build_v4_db(tmp_path / "t.db")
    normal_group(conn)
    conn.commit()
    monkeypatch.setitem(
        db.MIGRATIONS, 5, "CREATE TABLE group_windows(id INTEGER);\nSELECT * FROM no_such_table;"
    )

    with pytest.raises(sqlite3.Error):
        db.migrate(conn, TZ)

    assert db.current_version(conn) == 4
    assert (
        conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='group_windows'").fetchone()[0]
        == 0
    )
    conn.close()
