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


def test_user_pin_rules():
    for bad in ("0000", "123", "123456789", "abcd", ""):
        assert credentials.validate_user_pin(bad) is not None
    assert credentials.validate_user_pin("1234") is None
    assert credentials.validate_user_pin("12345678") is None


def test_set_and_verify_roundtrip(conn):
    credentials.set_secret(conn, "admin", "a good long password")
    stored = credentials.get_hash(conn, "admin")
    assert stored.startswith("$argon2id$")
    assert credentials.verify(conn, "admin", "a good long password")
    assert not credentials.verify(conn, "admin", "wrong")


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
    conn.execute("INSERT INTO credentials(role, hash, updated_at) VALUES('admin', ?, 0)", (legacy,))
    conn.commit()

    assert security.verify_secret(legacy, "legacy-secret")
    assert credentials.verify(conn, "admin", "legacy-secret")
    # it was transparently re-hashed to argon2id
    assert credentials.get_hash(conn, "admin").startswith("$argon2id$")
    assert credentials.verify(conn, "admin", "legacy-secret")


def test_reset_writes_a_usable_hash(tmp_path):
    path = tmp_path / "timefence.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()

    written = credentials.reset(tmp_path, "admin", "brand new password")
    assert written == path

    conn = db.connect(path)
    assert credentials.verify(conn, "admin", "brand new password")
    conn.close()


def test_reset_rejects_bad_values(tmp_path):
    with pytest.raises(credentials.CredentialError):
        credentials.reset(tmp_path, "admin", "short")
    with pytest.raises(credentials.CredentialError):
        credentials.reset(tmp_path, "user", "0000")
    with pytest.raises(credentials.CredentialError):
        credentials.reset(tmp_path, "nobody", "whatever")


def test_cli_passwd(tmp_path):
    path = tmp_path / "timefence.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "timefence",
            "passwd",
            "--role",
            "user",
            "--data-dir",
            str(tmp_path),
        ],
        input="1357\n1357\n",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "user credential updated" in result.stdout
    conn = db.connect(path)
    assert credentials.verify(conn, "user", "1357")
    conn.close()
