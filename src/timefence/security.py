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

# Key derivations are CPU- and memory-hungry: let at most two run at a time so
# a burst of logins cannot exhaust the box.
HASH_GATE = threading.BoundedSemaphore(2)


# --------------------------------------------------------------------------- #
# hashing
# --------------------------------------------------------------------------- #


def hash_secret(secret: str) -> str:
    """argon2id when available, stdlib scrypt otherwise."""
    with HASH_GATE:
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
    with HASH_GATE:
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
    username: str = ""
    uid: int = 0
    epoch: int = 0


class SessionManager:
    """Signed cookie: {role, csrf} - no server-side session store."""

    COOKIE = "timefence_session"

    def __init__(self, secret: str, max_age: int = 12 * 3600) -> None:
        self.serializer = URLSafeTimedSerializer(secret, salt="timefence-session")
        self.max_age = max_age

    def issue(
        self,
        role: str,
        username: str = "",
        uid: int = 0,
        epoch: int = 0,
        csrf: str | None = None,
    ) -> tuple[str, str]:
        """Sign a cookie; a re-issued cookie keeps the caller's existing csrf token."""
        token_csrf = csrf or secrets.token_urlsafe(24)
        token = self.serializer.dumps(
            {
                "role": role,
                "csrf": token_csrf,
                "username": username,
                "uid": uid,
                "epoch": epoch,
            }
        )
        return token, token_csrf

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
        uid = payload.get("uid")
        epoch = payload.get("epoch")
        if role not in {"admin", "user"} or not isinstance(csrf, str):
            return None
        # cookies from older releases have no account binding: sign in again
        if type(uid) is not int or type(epoch) is not int:
            return None
        username = payload.get("username") or ""
        return SessionData(role=role, csrf=csrf, username=str(username), uid=uid, epoch=epoch)


# --------------------------------------------------------------------------- #
# client IP / rate limiting
# --------------------------------------------------------------------------- #


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse an address, mapping ::ffff:127.0.0.1 to 127.0.0.1."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped or address


def _is_trusted(address, trusted: tuple) -> bool:
    for network in trusted:
        try:
            if address in network:
                return True
        except TypeError:  # an IPv4 address never matches an IPv6 network
            continue
    return False


def client_ip(peer: str | None, forwarded: str | None, trusted: tuple) -> str:
    """The client address: the right-most X-Forwarded-For hop we do not trust.

    `forwarded` is the joined value of every X-Forwarded-For header line. A hop
    we cannot parse means the header is untrustworthy and the peer is used.
    """
    peer_text = peer or "unknown"
    peer_address = _parse_ip(peer_text)
    if peer_address is None:
        return peer_text
    if not trusted or not _is_trusted(peer_address, trusted):
        return str(peer_address)
    hops = [part.strip() for part in (forwarded or "").split(",") if part.strip()]
    if not hops:
        return str(peer_address)
    for hop in reversed(hops):
        hop_address = _parse_ip(hop)
        if hop_address is None:
            return str(peer_address)
        if not _is_trusted(hop_address, trusted):
            return str(hop_address)
    return str(_parse_ip(hops[0]) or peer_address)


PAIR_THRESHOLD = 5
IP_THRESHOLD = 20
MAX_LIMITER_KEYS = 10_000


class LoginRateLimiter:
    """Failure counters keyed by `pair:<ip>|<username>` and `ip:<ip>`.

    A pair lock only stops that account from that address and is cleared by a
    successful login of the same pair; the address lock caps distributed
    guessing and is never cleared by a success. In memory by design: a restart
    clears lockouts, which is acceptable for a single-process household app.
    """

    def __init__(
        self,
        pair_threshold: int = PAIR_THRESHOLD,
        ip_threshold: int = IP_THRESHOLD,
        window: int = LOCK_SECONDS,
        max_keys: int = MAX_LIMITER_KEYS,
    ) -> None:
        self.pair_threshold = pair_threshold
        self.ip_threshold = ip_threshold
        self.window = window
        self.max_keys = max_keys
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._records = 0

    def threshold_for(self, key: str) -> int:
        return self.ip_threshold if key.startswith("ip:") else self.pair_threshold

    def _prune_key(self, key: str, now: float) -> list[float]:
        entries = [t for t in self._failures.get(key, []) if now - t < self.window]
        if entries:
            self._failures[key] = entries
        else:
            self._failures.pop(key, None)
        return entries

    def _prune_all(self, now: float) -> None:
        for key in list(self._failures):
            self._prune_key(key, now)

    def locked_for(self, key: str) -> int:
        """Seconds remaining on the lockout, 0 when not locked."""
        with self._lock:
            now = time.time()
            entries = self._prune_key(key, now)
            if len(entries) >= self.threshold_for(key):
                return max(0, int(entries[-1] + self.window - now))
            return 0

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = time.time()
            self._records += 1
            if self._records % 256 == 0:
                self._prune_all(now)
            entries = self._prune_key(key, now)
            entries.append(now)
            self._failures[key] = entries
            while len(self._failures) > self.max_keys:
                oldest = min(self._failures, key=lambda item: self._failures[item][-1])
                del self._failures[oldest]

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def keys(self) -> list[str]:
        """The keys currently held (diagnostics and tests)."""
        with self._lock:
            return list(self._failures)
