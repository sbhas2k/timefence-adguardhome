"""Configuration: everything comes from environment variables / .env."""

from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("timefence.config")


class ConfigError(RuntimeError):
    """Raised when the environment does not describe a usable configuration."""


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"expected an integer, got {value!r}") from exc


def normalize_base_path(value: str | None) -> str:
    """'timer' -> '/timer', '/' -> '/', '/timer/' -> '/timer'."""
    path = (value or "/").strip()
    if not path.startswith("/"):
        path = "/" + path
    path = "/" + path.strip("/")
    return "/" if path == "/" else path


def _parse_networks(value: str | None) -> tuple[ipaddress._BaseNetwork, ...]:
    nets: list[ipaddress._BaseNetwork] = []
    for raw in (value or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            nets.append(ipaddress.ip_network(raw, strict=False))
        except ValueError as exc:
            raise ConfigError(f"TRUSTED_PROXIES: {raw!r} is not a CIDR") from exc
    return tuple(nets)


@dataclass
class Settings:
    adguard_url: str = "http://127.0.0.1:3000"
    adguard_user: str = "admin"
    adguard_password: str = field(default="", repr=False)
    base_path: str = "/"
    tz: str = "UTC"
    listen_host: str = "127.0.0.1"
    listen_port: int = 8099
    data_dir: Path = Path("/var/lib/timefence")
    trusted_proxies: tuple = ()
    reconcile_interval: int = 30
    secure_cookies: bool = True
    log_level: str = "INFO"
    session_max_age: int = 12 * 3600

    # derived
    zone: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"), repr=False)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "timefence.db"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = dict(os.environ if env is None else env)

        password = e.get("ADGUARD_PASSWORD", "")
        password_file = e.get("ADGUARD_PASSWORD_FILE", "").strip()
        if password_file:
            try:
                password = Path(password_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ConfigError(
                    f"cannot read ADGUARD_PASSWORD_FILE {password_file}: {exc}"
                ) from exc

        s = cls(
            adguard_url=(e.get("ADGUARD_URL") or "http://127.0.0.1:3000").rstrip("/"),
            adguard_user=e.get("ADGUARD_USER") or "admin",
            adguard_password=password,
            base_path=normalize_base_path(e.get("BASE_PATH")),
            tz=(e.get("TZ") or "UTC").strip(),
            listen_host=e.get("LISTEN_HOST") or "127.0.0.1",
            listen_port=_int(e.get("LISTEN_PORT"), 8099),
            data_dir=Path(e.get("DATA_DIR") or "/var/lib/timefence"),
            trusted_proxies=_parse_networks(e.get("TRUSTED_PROXIES")),
            reconcile_interval=_int(e.get("RECONCILE_INTERVAL"), 30),
            secure_cookies=_bool(e.get("SECURE_COOKIES"), True),
            log_level=(e.get("LOG_LEVEL") or "INFO").upper(),
        )
        s.zone = cls._zone(s.tz)
        return s

    @staticmethod
    def _zone(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"TZ: unknown timezone {name!r}") from exc

    def validate(self) -> None:
        problems: list[str] = []
        if not self.adguard_url.startswith(("http://", "https://")):
            problems.append("ADGUARD_URL must start with http:// or https://")
        if not self.adguard_password:
            problems.append("ADGUARD_PASSWORD or ADGUARD_PASSWORD_FILE must be set")
        if not (1 <= self.listen_port <= 65535):
            problems.append("LISTEN_PORT must be 1..65535")
        if not self.base_path.startswith("/"):
            problems.append("BASE_PATH must start with /")
        if self.reconcile_interval < 5:
            problems.append("RECONCILE_INTERVAL must be >= 5 seconds")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            problems.append("LOG_LEVEL must be DEBUG/INFO/WARNING/ERROR/CRITICAL")
        try:
            self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:  # best effort: an existing directory keeps its mode otherwise
                os.chmod(self.data_dir, 0o700)
            except OSError as exc:
                log.warning("could not set mode 0700 on %s: %s", self.data_dir, exc)
        except OSError as exc:
            problems.append(f"DATA_DIR {self.data_dir} is not usable: {exc}")
        if problems:
            raise ConfigError("; ".join(problems))

    def redacted(self) -> dict:
        return {
            "adguard_url": self.adguard_url,
            "adguard_user": self.adguard_user,
            "adguard_password": "***" if self.adguard_password else "(unset)",
            "base_path": self.base_path,
            "tz": self.tz,
            "listen": f"{self.listen_host}:{self.listen_port}",
            "data_dir": str(self.data_dir),
            "trusted_proxies": [str(n) for n in self.trusted_proxies],
            "reconcile_interval": self.reconcile_interval,
            "secure_cookies": self.secure_cookies,
            "log_level": self.log_level,
        }
