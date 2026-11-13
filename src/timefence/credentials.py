"""Credential rules, hashing and reset helpers.

Used by the web app (login, setup, settings) and by the `timefence passwd`
recovery command.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from . import db, security

PIN_RE = re.compile(r"^\d{4,8}$")

DEFAULT_ADMIN_PASSWORD = "admin"
DEFAULT_USER_PIN = "0000"
ROLES = ("admin", "user")


class CredentialError(ValueError):
    """The requested role or secret is not acceptable."""


def validate_admin_password(value: str) -> str | None:
    """Return an error message, or None when the password is acceptable."""
    if len(value or "") < 10:
        return "The admin password must be at least 10 characters long."
    if value == DEFAULT_ADMIN_PASSWORD:
        return "The admin password must not be 'admin'."
    return None


def validate_user_pin(value: str) -> str | None:
    if not PIN_RE.match(value or "") or value == DEFAULT_USER_PIN:
        return "The PIN must be 4-8 digits and must not be 0000."
    return None


def validate(role: str, secret: str) -> str | None:
    if role == "admin":
        return validate_admin_password(secret)
    if role == "user":
        return validate_user_pin(secret)
    return f"unknown role {role!r}"


def get_hash(conn: sqlite3.Connection, role: str) -> str | None:
    row = conn.execute("SELECT hash FROM credentials WHERE role=?", (role,)).fetchone()
    return row["hash"] if row else None


def set_secret(conn: sqlite3.Connection, role: str, secret: str) -> None:
    conn.execute(
        "INSERT INTO credentials(role, hash, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(role) DO UPDATE SET hash=excluded.hash, updated_at=excluded.updated_at",
        (role, security.hash_secret(secret), int(time.time())),
    )
    conn.commit()


def verify(conn: sqlite3.Connection, role: str, secret: str) -> bool:
    """Check a secret and transparently upgrade legacy hash formats."""
    stored = get_hash(conn, role)
    if not stored or not security.verify_secret(stored, secret):
        return False
    if security.needs_rehash(stored):
        set_secret(conn, role, secret)
    return True


def db_path_for(data_dir: str | Path) -> Path:
    return Path(data_dir) / "timefence.db"


def reset(data_dir: str | Path, role: str, secret: str) -> Path:
    """Set a new secret offline (service may be running; SQLite handles it)."""
    if role not in ROLES:
        raise CredentialError(f"role must be one of {', '.join(ROLES)}")
    problem = validate(role, secret)
    if problem:
        raise CredentialError(problem)
    path = db_path_for(data_dir)
    if not path.exists():
        raise CredentialError(f"database not found: {path}")
    conn = db.connect(path)
    try:
        db.migrate(conn)
        set_secret(conn, role, secret)
    finally:
        conn.close()
    return path
