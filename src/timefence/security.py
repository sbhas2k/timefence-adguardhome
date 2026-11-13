"""Password hashing, sessions, CSRF and login rate limiting."""

from __future__ import annotations

import binascii
import hashlib
import hmac
import ipaddress
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

try:  # preferred
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerifyMismatchError

    _ARGON2 = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)
except Exception:  # pragma: no cover - argon2-cffi is a hard dependency in practice
    _ARGON2 = None
    InvalidHashError = VerifyMismatchError = Exception  # type: ignore[assignment,misc]


LOCK_THRESHOLD = 5
LOCK_SECONDS = 15 * 60


# --------------------------------------------------------------------------- #
# hashing
# --------------------------------------------------------------------------- #


def hash_secret(secret: str) -> str:
    """argon2id when available, stdlib scrypt otherwise."""
    if _ARGON2 is not None:
        return _ARGON2.hash(secret)
    salt = os.urandom(16)
    key = hashlib.scrypt(secret.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"$scrypt$14$8$1${salt.hex()}${key.hex()}"


def _verify_scrypt(stored: str, secret: str) -> bool:
    """Accepts our own $scrypt$... and Werkzeug's scrypt:N:r:p$salt$hex."""
    try:
        if stored.startswith("$scrypt$"):
            _, _tag, logn, r, p, salt_hex, key_hex = stored.split("$")
            key = hashlib.scrypt(
                secret.encode(),
                salt=binascii.unhexlify(salt_hex),
                n=2 ** int(logn),
                r=int(r),
                p=int(p),
                dklen=len(key_hex) // 2,
            )
            return hmac.compare_digest(key.hex(), key_hex)
        # werkzeug: scrypt:32768:8:1$salt$hexdigest
        method, salt, digest = stored.split("$", 2)
        _, n, r, p = method.split(":")
        key = hashlib.scrypt(
            secret.encode(),
            salt=salt.encode(),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(digest) // 2,
            maxmem=132 * 1024 * 1024,
        )
        return hmac.compare_digest(key.hex(), digest)
    except (ValueError, TypeError):
        return False


def _verify_pbkdf2(stored: str, secret: str) -> bool:
    """Werkzeug pbkdf2:sha256:600000$salt$hexdigest (legacy migration support)."""
    try:
        method, salt, digest = stored.split("$", 2)
        _, algo, iterations = method.split(":")
        h = hashlib.pbkdf2_hmac(algo, secret.encode(), salt.encode(), int(iterations))
        return hmac.compare_digest(h.hex(), digest)
    except (ValueError, TypeError):
        return False


def verify_secret(stored: str, secret: str) -> bool:
    if not stored:
        return False
    if stored.startswith("$argon2") and _ARGON2 is not None:
        try:
            return _ARGON2.verify(stored, secret)
        except (VerifyMismatchError, InvalidHashError):
            return False
    if stored.startswith(("$scrypt$", "scrypt:")):
        return _verify_scrypt(stored, secret)
    if stored.startswith("pbkdf2:"):
        return _verify_pbkdf2(stored, secret)
    return False


def needs_rehash(stored: str) -> bool:
    """True when the stored hash is not the modern default (argon2id)."""
    return _ARGON2 is not None and not stored.startswith("$argon2")


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #


def load_or_create_secret(data_dir: Path) -> str:
    path = Path(data_dir) / "session-secret"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except FileNotFoundError:
        pass
    value = secrets.token_urlsafe(48)
    path.write_text(value + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return value


@dataclass
class SessionData:
    role: str
    csrf: str


class SessionManager:
    """Signed cookie: {role, csrf} - no server-side session store."""

    COOKIE = "timefence_session"

    def __init__(self, secret: str, max_age: int = 12 * 3600) -> None:
        self.serializer = URLSafeTimedSerializer(secret, salt="timefence-session")
        self.max_age = max_age

    def issue(self, role: str) -> tuple[str, str]:
        csrf = secrets.token_urlsafe(24)
        token = self.serializer.dumps({"role": role, "csrf": csrf})
        return token, csrf

    def read(self, token: str | None) -> SessionData | None:
        if not token:
            return None
        try:
            payload = self.serializer.loads(token, max_age=self.max_age)
        except (BadSignature, SignatureExpired):
            return None
        if not isinstance(payload, dict):
            return None
        role = payload.get("role")
        csrf = payload.get("csrf")
        if role not in {"admin", "user"} or not isinstance(csrf, str):
            return None
        return SessionData(role=role, csrf=csrf)


# --------------------------------------------------------------------------- #
# client IP / rate limiting
# --------------------------------------------------------------------------- #


def client_ip(host: str | None, headers: dict[str, str], trusted: tuple) -> str:
    """X-Forwarded-For is honoured only when the peer is a trusted proxy."""
    peer = host or "unknown"
    if not trusted:
        return peer
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_ip in net for net in trusted):
        return peer
    forwarded = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        try:
            return str(ipaddress.ip_address(first))
        except ValueError:
            return peer
    return peer


class LoginRateLimiter:
    """5 failures within 15 minutes lock the client IP for 15 minutes.

    In-memory by design: a restart clears lockouts, which is acceptable for a
    single-process household app and avoids writing attacker-controlled data
    into the database.
    """

    def __init__(self, threshold: int = LOCK_THRESHOLD, window: int = LOCK_SECONDS) -> None:
        self.threshold = threshold
        self.window = window
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, ip: str, now: float) -> list[float]:
        entries = [t for t in self._failures.get(ip, []) if now - t < self.window]
        if entries:
            self._failures[ip] = entries
        else:
            self._failures.pop(ip, None)
        return entries

    def locked_for(self, ip: str) -> int:
        """Seconds remaining on the lockout, 0 when not locked."""
        with self._lock:
            now = time.time()
            entries = self._prune(ip, now)
            if len(entries) >= self.threshold:
                return max(0, int(entries[-1] + self.window - now))
            return 0

    def record_failure(self, ip: str) -> None:
        with self._lock:
            now = time.time()
            entries = self._prune(ip, now)
            entries.append(now)
            self._failures[ip] = entries

    def reset(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)
