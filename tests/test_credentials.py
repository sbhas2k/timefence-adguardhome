"""Credential rules, verification (incl. legacy formats) and the reset command."""

from __future__ import annotations

import subprocess
import sys

import pytest

from timefence import credentials, db, security


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "c.db")
    db.migrate(connection)
    yield connection
    connection.close()


def test_admin_password_rules():
    assert credentials.validate_admin_password("admin") is not None
    assert credentials.validate_admin_password("short") is not None
    assert credentials.validate_admin_password("a good long password") is None


def test_set_and_verify_roundtrip(conn):
    stored = security.hash_secret("a good long password")
    conn.execute(
        "INSERT INTO users(username, role, hash, created_at, updated_at) "
        "VALUES('admin','admin',?,0,0)",
        (stored,),
    )
    conn.commit()
    row = conn.execute("SELECT hash FROM users WHERE username='admin'").fetchone()
    assert row["hash"].startswith("$argon2id$")
    assert security.verify_secret(row["hash"], "a good long password")
    assert not security.verify_secret(row["hash"], "wrong")


def test_werkzeug_scrypt_hash_still_verifies_and_upgrades(conn):
    """The pre-0.1.0 app used Werkzeug hashes; they must keep working."""
    salt = "AbCdEfGhIjKlMn"
    digest = (
        __import__("hashlib")
        .scrypt(
            b"legacy-secret",
            salt=salt.encode(),
            n=32768,
            r=8,
            p=1,
            dklen=64,
            maxmem=132 * 1024 * 1024,
        )
        .hex()
    )
    legacy = f"scrypt:32768:8:1${salt}${digest}"

    assert security.verify_secret(legacy, "legacy-secret")
    assert not security.verify_secret(legacy, "wrong")
    # the login path sees that the stored hash is old and re-hashes it
    assert security.needs_rehash(legacy) is True
    assert security.needs_rehash(security.hash_secret("legacy-secret")) is False


def test_reset_writes_a_usable_hash(tmp_path):
    path = tmp_path / "timefence.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO users(username, role, hash, created_at, updated_at) "
        "VALUES('admin','admin',?,0,0)",
        (security.hash_secret("old-password"),),
    )
    conn.commit()
    conn.close()

    written = credentials.reset(tmp_path, "admin", "brand new password")
    assert written == path

    conn = db.connect(path)
    stored = conn.execute("SELECT hash FROM users WHERE username='admin'").fetchone()[0]
    assert security.verify_secret(stored, "brand new password")
    conn.close()


def test_reset_rejects_bad_values(tmp_path):
    with pytest.raises(credentials.CredentialError):
        credentials.reset(tmp_path, "admin", "short")
    with pytest.raises(credentials.CredentialError):
        credentials.reset(tmp_path, "user", "short")
    with pytest.raises(credentials.CredentialError):
        credentials.reset(tmp_path, "nobody", "whatever")


def test_cli_passwd(tmp_path):
    path = tmp_path / "timefence.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO users(username, role, hash, created_at, updated_at) "
        "VALUES('admin','admin',?,0,0)",
        (security.hash_secret("old-password"),),
    )
    conn.commit()
    conn.close()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "timefence",
            "passwd",
            "--role",
            "admin",
            "--data-dir",
            str(tmp_path),
        ],
        input="new-password-123\nnew-password-123\n",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "updated" in result.stdout
    conn = db.connect(path)
    stored = conn.execute("SELECT hash FROM users WHERE username='admin'").fetchone()[0]
    assert security.verify_secret(stored, "new-password-123")
    conn.close()


def test_reset_refuses_a_role_that_does_not_match_the_row(tmp_path):
    path = tmp_path / "timefence.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.execute(
        "INSERT INTO users(username, role, hash, created_at, updated_at) "
        "VALUES('kid','user',?,0,0)",
        (security.hash_secret("kid-password"),),
    )
    conn.commit()
    conn.close()

    # an admin reset aimed at a user row is refused, not silently applied
    with pytest.raises(credentials.CredentialError, match="role"):
        credentials.reset(tmp_path, "admin", "brand new password", username="kid")
    # and the stored rules decide the length: eight characters are fine for a user
    credentials.reset(tmp_path, "user", "eightchr", username="kid")
    conn = db.connect(path)
    stored = conn.execute("SELECT hash FROM users WHERE username='kid'").fetchone()[0]
    assert security.verify_secret(stored, "eightchr")
    conn.close()


def test_cli_reset_refuses_to_migrate_v3(tmp_path):
    from zoneinfo import ZoneInfo

    path = tmp_path / "timefence.db"
    conn = db.connect(path)
    assert db.migrate(conn, ZoneInfo("UTC"), target=3) == 3
    conn.execute(
        "INSERT INTO groups(name, emoji, weekday_minutes, weekend_minutes, paused, deleted, "
        "created_at, mode) VALUES('Sessions', '', 30, 30, 0, 0, 0, 'session')"
    )
    conn.commit()
    conn.close()

    with pytest.raises(credentials.CredentialError, match="start TimeFence once"):
        credentials.reset(tmp_path, "admin", "brand new password")

    conn = db.connect(path)
    assert db.current_version(conn) == 3
    assert [row["mode"] for row in conn.execute("SELECT mode FROM groups")] == ["session"]
    conn.close()
