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

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")

DEFAULT_ADMIN_PASSWORD = "admin"
ROLES = ("admin", "user")


class CredentialError(ValueError):
    """The requested role or secret is not acceptable."""


def validate_username(value: str) -> str | None:
    if not USERNAME_RE.match(value or ""):
        return "Username must be 3-32 characters: letters, digits, dot, dash, underscore."
    return None


def validate_admin_password(value: str) -> str | None:
    """Return an error message, or None when the password is acceptable."""
    if len(value or "") < 10:
        return "The admin password must be at least 10 characters long."
    if value == DEFAULT_ADMIN_PASSWORD:
        return "The admin password must not be 'admin'."
    return None


def validate(role: str, secret: str) -> str | None:
    if role == "admin":
        return validate_admin_password(secret)
    if role == "user":
        if len(secret or "") < 8:
            return "The password must be at least 8 characters long."
        return None
    return f"unknown role {role!r}"


def bump_epoch(conn: sqlite3.Connection, user_id: int) -> None:
    """Invalidate every cookie issued for this account (committed by the caller)."""
    conn.execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE id=?", (user_id,))


def store_password(conn: sqlite3.Connection, user_id: int, new_hash: str) -> None:
    """Write an already-hashed password and sign the account out everywhere.

    The caller hashes outside the store lock and commits.
    """
    conn.execute(
        "UPDATE users SET hash=?, updated_at=? WHERE id=?",
        (new_hash, int(time.time()), user_id),
    )
    bump_epoch(conn, user_id)


def db_path_for(data_dir: str | Path) -> Path:
    return Path(data_dir) / "timefence.db"


def reset(data_dir: str | Path, role: str, secret: str, username: str = "admin") -> Path:
    """Set a new password offline (service may be running; SQLite handles it)."""
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
        try:
            db.migrate(conn, allow_data_migration=False)
        except db.DataMigrationRequired as exc:
            raise CredentialError(str(exc)) from exc
        row = conn.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        if not row:
            raise CredentialError(f"no account called {username!r}")
        store_password(conn, row["id"], security.hash_secret(secret))
        conn.commit()
    finally:
        conn.close()
    return path
