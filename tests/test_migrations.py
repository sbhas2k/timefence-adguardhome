"""Schema migrations: atomic steps, the session-mode removal, the budget removal."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from timefence import db

TZ = ZoneInfo("Europe/London")


def build_v3_db(path) -> sqlite3.Connection:
    conn = db.connect(path)
    assert db.migrate(conn, TZ, target=3) == 3
    return conn


def build_v4_db(path) -> sqlite3.Connection:
    conn = db.connect(path)
    assert db.migrate(conn, TZ, target=4) == 4
    return conn


def build_v5_db(path) -> sqlite3.Connection:
    conn = db.connect(path)
    assert db.migrate(conn, TZ, target=5) == 5
    return conn


def build_v6_db(path) -> sqlite3.Connection:
    conn = db.connect(path)
    assert db.migrate(conn, TZ, target=6) == 6
    return conn


def add_group(conn, name, *, mode="session", deleted=0, created_at=0) -> int:
    cur = conn.execute(
        "INSERT INTO groups(name, emoji, weekday_minutes, weekend_minutes, paused, deleted, "
        "created_at, mode) VALUES(?,?,?,?,0,?,?,?)",
        (name, "", 60, 60, deleted, created_at, mode),
    )
    return int(cur.lastrowid)


def normal_group(conn, name="YouTube") -> int:
    cur = conn.execute(
        "INSERT INTO groups(name, emoji, weekday_minutes, weekend_minutes, paused, deleted, "
        "created_at) VALUES(?,?,?,?,0,0,0)",
        (name, "", 60, 60),
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


def table_names(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def group_columns(conn) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(groups)")}


# --------------------------------------------------------------------------- #
# v4: sessions are gone
# --------------------------------------------------------------------------- #


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

    assert db.migrate(conn, TZ, target=5) == 5

    assert db.current_version(conn) == 5
    assert {row[0] for row in conn.execute("SELECT DISTINCT mode FROM groups")} == {"auto"}
    assert usage(conn, session_group, d1) == 900
    assert usage(conn, session_group, d2) == 120
    assert usage(conn, auto_group, d1) == 500
    assert usage(conn, deleted_group, d1) == 60
    conn.close()


@pytest.mark.parametrize("hour, minute", [(1, 1), (12, 0), (23, 59)])
def test_open_sessions_are_closed_and_billed_elapsed(tmp_path, monkeypatch, hour, minute):
    conn = build_v3_db(tmp_path / "t.db")
    group = add_group(conn, "Open")
    # a fixed local instant that is never within an hour of midnight, so both
    # sessions below are the same local day (with the real clock this test
    # failed whenever the suite ran between local 00:00 and 01:00)
    now = local_ts("2026-11-10", hour=hour) + minute * 60
    monkeypatch.setattr(db, "_clock", lambda: now)
    add_session(conn, group, now - 600, 0, planned=900, ended=False)
    add_session(conn, group, now - 3600, 0, planned=900, ended=False)

    db.migrate(conn, TZ, target=5)

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


@pytest.mark.parametrize("hour, minute", [(0, 5), (0, 30), (0, 59)])
def test_open_sessions_are_billed_to_the_day_each_one_started(tmp_path, monkeypatch, hour, minute):
    conn = build_v3_db(tmp_path / "t.db")
    group = add_group(conn, "Open")
    now = local_ts("2026-11-10", hour=hour) + minute * 60
    monkeypatch.setattr(db, "_clock", lambda: now)
    add_session(conn, group, now - 60, 0, planned=900, ended=False)
    add_session(conn, group, now - 3600, 0, planned=900, ended=False)

    db.migrate(conn, TZ, target=5)

    rows = list(conn.execute("SELECT used_seconds FROM sessions ORDER BY id"))
    assert [row["used_seconds"] for row in rows] == [60, 900]
    assert usage(conn, group, "2026-11-09") == 900  # the hour that started yesterday
    assert usage(conn, group, "2026-11-10") == 60
    conn.close()


def test_session_started_before_midnight_is_billed_to_its_start_day(tmp_path):
    conn = build_v3_db(tmp_path / "t.db")
    group = add_group(conn, "Late")
    late = local_ts("2026-11-10", hour=23) + 58 * 60
    add_session(conn, group, late, 300)

    db.migrate(conn, TZ, target=5)

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

    db.migrate(conn, TZ, target=5)

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
    # a fixed clock two weeks later: the open session is billed to its own day,
    # never to the 10th, so the closed row's day cannot drift with the wall clock
    later = local_ts("2026-11-24", hour=12)
    add_session(conn, group, later - 600, 0, planned=900, ended=False)
    monkeypatch.setattr(db, "_clock", lambda: later)
    conn.commit()

    def failing(connection, tz):
        db._close_open_sessions(connection, db._clock())
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

    assert db.migrate(conn, TZ, target=5) == 5
    assert usage(conn, group, "2026-11-10") == 300
    assert db.migrate(conn, TZ) == 7
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
    assert "owner_id" not in group_columns(conn)
    conn.close()


# --------------------------------------------------------------------------- #
# the whole chain, idempotency, refusals
# --------------------------------------------------------------------------- #


def test_fresh_db_reaches_v7_without_a_backup(tmp_path):
    path = tmp_path / "fresh.db"
    conn = db.connect(path)

    assert db.migrate(conn, TZ) == 7
    assert db.current_version(conn) == 7
    for goal in (4, 5, 6, 7):
        assert not path.with_name(f"{path.name}.pre-v{goal}.bak").exists()
    assert "bonus" not in table_names(conn)
    assert "usage_seconds" not in table_names(conn)
    assert not {"weekday_minutes", "weekend_minutes"} & group_columns(conn)
    # steps 5 and 7 both create group_windows on a fresh database; the v7 shape wins
    assert {row[1] for row in conn.execute("PRAGMA table_info(group_windows)")} == {
        "id",
        "group_id",
        "start_min",
        "end_min",
        "days",
    }
    assert conn.execute("SELECT COUNT(*) FROM group_windows").fetchone()[0] == 0
    conn.close()


def test_newer_db_is_refused(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("PRAGMA user_version=8")
    conn.commit()

    with pytest.raises(RuntimeError, match="newer than this build"):
        db.migrate(conn, TZ)
    assert db.current_version(conn) == 8
    conn.close()


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "t.db"
    conn = build_v4_db(path)
    normal_group(conn)
    conn.commit()

    assert db.migrate(conn, TZ) == 7
    backup = path.with_name(path.name + ".pre-v7.bak")
    assert backup.exists()
    stamp = backup.stat().st_mtime_ns

    assert db.migrate(conn, TZ) == 7
    # a second call applies nothing, so it must not rewrite the copy
    assert backup.stat().st_mtime_ns == stamp
    assert not path.with_name(path.name + ".pre-v4.bak").exists()
    assert not path.with_name(path.name + ".pre-v5.bak").exists()
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


def test_passwd_refuses_an_older_db_until_the_server_upgraded_it(tmp_path):
    """v7 rewrites window rows, so an offline command may not run it."""
    path = tmp_path / "t.db"
    conn = build_v4_db(path)
    normal_group(conn)
    conn.commit()

    with pytest.raises(db.DataMigrationRequired, match="start TimeFence once"):
        db.migrate(conn, TZ, allow_data_migration=False)
    assert db.current_version(conn) == 4

    # the server runs the upgrade; afterwards the pure-DDL check passes again
    assert db.migrate(conn, TZ) == 7
    assert db.migrate(conn, TZ, allow_data_migration=False) == 7
    assert path.with_name(path.name + ".pre-v7.bak").exists()
    conn.close()


# --------------------------------------------------------------------------- #
# schema v5: windows and alert de-duplication
# --------------------------------------------------------------------------- #


def test_v5_creates_the_window_and_alert_tables(tmp_path):
    conn = build_v4_db(tmp_path / "t.db")

    assert db.migrate(conn, TZ, target=5) == 5
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

    assert db.migrate(conn, TZ, target=5) == 5

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

    db.migrate(conn, TZ, target=5)

    with sqlite3.connect(backup) as check:
        assert int(check.execute("PRAGMA user_version").fetchone()[0]) == 4
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert check.execute("SELECT name FROM groups").fetchone()[0] == "YouTube"
    if os.name != "nt":
        assert backup.stat().st_mode & 0o777 == 0o600
    conn.close()


def test_migration_aborts_when_the_upgrade_backup_fails(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    conn = build_v4_db(path)
    normal_group(conn)
    conn.commit()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(db.os, "replace", boom)

    with pytest.raises(RuntimeError) as excinfo:
        db.migrate(conn, TZ, target=5)

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
        db.migrate(conn, TZ, target=5)

    assert db.current_version(conn) == 4
    assert (
        conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='group_windows'").fetchone()[0]
        == 0
    )
    conn.close()


# --------------------------------------------------------------------------- #
# schema v6: the minutes budget and metering are gone
# --------------------------------------------------------------------------- #


def test_v6_drops_the_budget_tables_and_columns(tmp_path):
    conn = build_v5_db(tmp_path / "t.db")
    group = normal_group(conn, "Games")
    conn.execute(
        "INSERT INTO group_windows(group_id, start_min, end_min, days) VALUES(?,?,?,?)",
        (group, 1260, 420, "all"),
    )
    conn.execute(
        "INSERT INTO usage_seconds(day, group_id, seconds) VALUES('2026-11-10', ?, 600)", (group,)
    )
    conn.execute("INSERT INTO bonus(day, group_id, seconds) VALUES('2026-11-10', ?, 600)", (group,))
    conn.execute("INSERT INTO free_allow(group_id, until_ts) VALUES(?, 1800000000)", (group,))
    conn.commit()

    assert db.migrate(conn, TZ) == 7
    assert db.current_version(conn) == 7

    assert "bonus" not in table_names(conn)
    assert "usage_seconds" not in table_names(conn)
    assert not {"weekday_minutes", "weekend_minutes"} & group_columns(conn)
    # the old block windows would silently invert their meaning; they are cleared
    assert conn.execute("SELECT COUNT(*) FROM group_windows").fetchone()[0] == 0

    # everything else survives: the group, its owner slot and free time
    row = conn.execute("SELECT name, emoji, paused, deleted, owner_id FROM groups").fetchone()
    assert tuple(row) == ("Games", "", 0, 0, None)
    assert conn.execute("SELECT COUNT(*) FROM free_allow").fetchone()[0] == 1
    assert "alerts_sent" in table_names(conn)
    conn.close()


def test_v5_to_v6_writes_a_verified_pre_v6_backup(tmp_path):
    path = tmp_path / "t.db"
    conn = build_v5_db(path)
    group = normal_group(conn, "Games")
    conn.execute(
        "INSERT INTO group_windows(group_id, start_min, end_min, days) VALUES(?,?,?,?)",
        (group, 1260, 420, "weekday"),
    )
    conn.execute(
        "INSERT INTO usage_seconds(day, group_id, seconds) VALUES('2026-11-10', ?, 900)", (group,)
    )
    conn.commit()
    backup = path.with_name(path.name + ".pre-v6.bak")
    backup.write_bytes(b"corrupt, not a database")

    db.migrate(conn, TZ, target=6)

    with sqlite3.connect(backup) as check:
        assert int(check.execute("PRAGMA user_version").fetchone()[0]) == 5
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert check.execute("SELECT seconds FROM usage_seconds").fetchone()[0] == 900
        assert check.execute("SELECT days FROM group_windows").fetchone()[0] == "weekday"
    if os.name != "nt":
        assert backup.stat().st_mode & 0o777 == 0o600
    conn.close()


def test_v6_step_is_a_plain_script_so_the_cli_can_open(tmp_path):
    assert isinstance(db.MIGRATIONS[6], str)
    path = tmp_path / "t.db"
    conn = build_v5_db(path)
    normal_group(conn)
    conn.commit()

    # only step 6 is pending, so the CLI may run it; v7 is a callable step
    assert db.migrate(conn, TZ, target=6, allow_data_migration=False) == 6
    assert path.with_name(path.name + ".pre-v6.bak").exists()
    conn.close()


def test_v6_failure_leaves_v5_intact(tmp_path, monkeypatch):
    conn = build_v5_db(tmp_path / "t.db")
    normal_group(conn)
    conn.commit()
    monkeypatch.setitem(
        db.MIGRATIONS,
        6,
        "DROP TABLE usage_seconds;\nSELECT * FROM no_such_table;",
    )

    with pytest.raises(sqlite3.Error):
        db.migrate(conn, TZ)

    assert db.current_version(conn) == 5
    assert "usage_seconds" in table_names(conn)
    assert {"weekday_minutes", "weekend_minutes"} <= group_columns(conn)
    conn.close()


# --------------------------------------------------------------------------- #
# schema v7: a window can name any days of the week
# --------------------------------------------------------------------------- #


def v6_group(conn, name="Games") -> int:
    """A group row as it looks at v6: the minute columns are already gone."""
    cur = conn.execute(
        "INSERT INTO groups(name, emoji, paused, deleted, created_at) VALUES(?,?,0,0,0)",
        (name, ""),
    )
    return int(cur.lastrowid)


def add_window(conn, group_id, start_min, end_min, days) -> int:
    cur = conn.execute(
        "INSERT INTO group_windows(group_id, start_min, end_min, days) VALUES(?,?,?,?)",
        (group_id, start_min, end_min, days),
    )
    return int(cur.lastrowid)


def window_rows(conn) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT id, group_id, start_min, end_min, days FROM group_windows ORDER BY id"
        )
    ]


def test_v6_to_v7_maps_every_old_day_value(tmp_path):
    conn = build_v6_db(tmp_path / "t.db")
    group = v6_group(conn)
    all_id = add_window(conn, group, 1260, 420, "all")
    weekday_id = add_window(conn, group, 360, 420, "weekday")
    weekend_id = add_window(conn, group, 720, 780, "weekend")
    conn.commit()

    assert db.migrate(conn, TZ) == 7
    assert db.current_version(conn) == 7
    assert window_rows(conn) == [
        (all_id, group, 1260, 420, "all"),
        (weekday_id, group, 360, 420, "mon,tue,wed,thu,fri"),
        (weekend_id, group, 720, 780, "sat,sun"),
    ]
    # the table shape is the v7 one, with the index back on the new table
    assert {row[1] for row in conn.execute("PRAGMA table_info(group_windows)")} == {
        "id",
        "group_id",
        "start_min",
        "end_min",
        "days",
    }
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(group_windows)")}
    assert "group_windows_group" in indexes
    # the new column carries no CHECK list: any day list is storable
    add_window(conn, group, 0, 60, "mon,fri")
    conn.commit()
    assert window_rows(conn)[-1][4] == "mon,fri"
    # the foreign key still cascades
    conn.execute("DELETE FROM groups WHERE id=?", (group,))
    assert window_rows(conn) == []
    conn.close()


def test_v6_to_v7_writes_a_verified_pre_v7_backup(tmp_path):
    path = tmp_path / "t.db"
    conn = build_v6_db(path)
    group = v6_group(conn)
    add_window(conn, group, 1260, 420, "weekday")
    conn.commit()
    backup = path.with_name(path.name + ".pre-v7.bak")
    backup.write_bytes(b"corrupt, not a database")

    db.migrate(conn, TZ)

    with sqlite3.connect(backup) as check:
        assert int(check.execute("PRAGMA user_version").fetchone()[0]) == 6
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert check.execute("SELECT days FROM group_windows").fetchone()[0] == "weekday"
    if os.name != "nt":
        assert backup.stat().st_mode & 0o777 == 0o600
    conn.close()


def test_v7_is_a_callable_step_so_the_cli_must_wait(tmp_path):
    assert callable(db.MIGRATIONS[7])
    conn = build_v6_db(tmp_path / "t.db")
    v6_group(conn)
    conn.commit()

    with pytest.raises(db.DataMigrationRequired):
        db.migrate(conn, TZ, allow_data_migration=False)
    assert db.current_version(conn) == 6
    assert db.migrate(conn, TZ) == 7
    conn.close()


def test_v7_failure_leaves_v6_intact(tmp_path, monkeypatch):
    conn = build_v6_db(tmp_path / "t.db")
    group = v6_group(conn)
    add_window(conn, group, 1260, 420, "weekend")
    conn.commit()

    def failing(connection, tz):
        connection.execute(db.MIGRATION_7_CREATE)
        connection.execute(db.MIGRATION_7_COPY)
        raise RuntimeError("boom")

    monkeypatch.setitem(db.MIGRATIONS, 7, failing)

    with pytest.raises(RuntimeError, match="boom"):
        db.migrate(conn, TZ)

    assert db.current_version(conn) == 6
    assert window_rows(conn) == [(1, group, 1260, 420, "weekend")]
    assert (
        conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='group_windows_v7'").fetchone()[
            0
        ]
        == 0
    )
    conn.close()


def test_the_runtime_sqlite_can_drop_a_column():
    """`ALTER TABLE ... DROP COLUMN` needs SQLite 3.35 (Python 3.12 ships 3.40+)."""
    assert sqlite3.sqlite_version_info >= (3, 35, 0), sqlite3.sqlite_version
