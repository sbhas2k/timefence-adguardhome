"""Effective timezone: TZ first, then the host's timezone, then the UTC fallback."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from timefence import i18n
from timefence.api import create_app
from timefence.config import ConfigError, Settings, detect_timezone

from .fakes import FakeAdGuard

ADMIN = {"username": "admin", "password": "admin"}
ADMIN2 = {"username": "admin", "password": "correct horse battery"}


def base_env(data_dir: Path, **over) -> dict:
    env = {
        "ADGUARD_URL": "http://127.0.0.1:3000",
        "ADGUARD_USER": "admin",
        "ADGUARD_PASSWORD": "secret",
        "BASE_PATH": "/",
        "DATA_DIR": str(data_dir),
        "SECURE_COOKIES": "false",
        "RECONCILE_INTERVAL": "60",
        "LOG_LEVEL": "WARNING",
    }
    env.update(over)
    return env


def zone_file(root: Path, name: str) -> Path:
    """A stand-in for /usr/share/zoneinfo/<name>; only its path matters."""
    path = root / "zoneinfo" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def admin_warnings(settings, fake) -> list[str]:
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        login = client.post("/api/login", json=ADMIN)
        assert login.status_code == 200, login.text
        client.headers.update({"X-CSRF-Token": login.json()["csrf"]})
        assert (
            client.post("/api/setup", json={"admin_password": ADMIN2["password"]}).status_code
            == 200
        )
        login = client.post("/api/login", json=ADMIN2)
        assert login.status_code == 200, login.text
        client.headers.update({"X-CSRF-Token": login.json()["csrf"]})
        return client.get("/api/state").json()["warnings"]


# --------------------------------------------------------------------------- #
# the pure detection function
# --------------------------------------------------------------------------- #


def test_an_explicit_tz_wins(tmp_path):
    timezone = tmp_path / "timezone"
    timezone.write_text("Asia/Tokyo\n")
    localtime = zone_file(tmp_path, "Europe/Berlin")

    assert detect_timezone("Europe/London", timezone_file=timezone, localtime_file=localtime) == (
        "Europe/London",
        "env",
    )
    # an explicit UTC is a choice, not a fallback
    assert detect_timezone("UTC", timezone_file=timezone, localtime_file=localtime) == (
        "UTC",
        "env",
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (":Europe/London", "Europe/London"),
        ("/usr/share/zoneinfo/Europe/London", "Europe/London"),
        (" Europe/London ", "Europe/London"),
    ],
)
def test_tz_path_forms_are_normalised(tmp_path, raw, expected):
    assert detect_timezone(
        raw, timezone_file=tmp_path / "none", localtime_file=tmp_path / "none"
    ) == (expected, "env")


def test_the_timezone_file_is_detected(tmp_path):
    timezone = tmp_path / "timezone"
    timezone.write_text("Asia/Tokyo\nignored second line\n")

    assert detect_timezone(None, timezone_file=timezone, localtime_file=tmp_path / "none") == (
        "Asia/Tokyo",
        "system",
    )


def test_the_localtime_path_is_detected(tmp_path):
    localtime = zone_file(tmp_path, "Europe/Berlin")

    assert detect_timezone("", timezone_file=tmp_path / "none", localtime_file=localtime) == (
        "Europe/Berlin",
        "system",
    )


def test_the_localtime_symlink_is_detected(tmp_path):
    target = zone_file(tmp_path, "Europe/Berlin")
    link = tmp_path / "localtime"
    try:
        link.symlink_to(target)
        localtime = link
    except OSError:
        # No symlink privilege on this machine: the resolved target is the case
        # that matters, so assert it directly instead of skipping.
        localtime = target

    assert detect_timezone(None, timezone_file=tmp_path / "none", localtime_file=localtime) == (
        "Europe/Berlin",
        "system",
    )


def test_invalid_detected_values_are_ignored(tmp_path):
    timezone = tmp_path / "timezone"
    timezone.write_text("Not/AZone\n")
    localtime = zone_file(tmp_path, "Elsewhere/Nope")

    assert detect_timezone(None, timezone_file=timezone, localtime_file=localtime) == (
        "UTC",
        "default",
    )


def test_nothing_found_falls_back_to_utc(tmp_path):
    assert detect_timezone(
        None, timezone_file=tmp_path / "none", localtime_file=tmp_path / "none"
    ) == ("UTC", "default")


def test_an_unknown_tz_still_raises():
    with pytest.raises(ConfigError, match="TZ: unknown timezone"):
        Settings.from_env(base_env(Path("/tmp/timefence-tz-test"), TZ="Mars/Olympus"))


# --------------------------------------------------------------------------- #
# settings and the admin warning
# --------------------------------------------------------------------------- #


def test_settings_record_the_detection_source(tmp_path):
    localtime = zone_file(tmp_path, "Asia/Tokyo")
    detected = Settings.from_env(
        base_env(tmp_path / "d1"),
        timezone_file=tmp_path / "none",
        localtime_file=localtime,
    )
    assert (detected.tz, detected.tz_source) == ("Asia/Tokyo", "system")
    assert detected.zone.key == "Asia/Tokyo"

    explicit = Settings.from_env(
        base_env(tmp_path / "d2", TZ="UTC"),
        timezone_file=tmp_path / "none",
        localtime_file=localtime,
    )
    assert (explicit.tz, explicit.tz_source) == ("UTC", "env")


def test_the_test_settings_pass_tz_explicitly(settings):
    assert settings.tz == "UTC"
    assert settings.tz_source == "env"


def test_the_dev_server_passes_tz_explicitly():
    source = (Path(__file__).resolve().parent.parent / "scripts" / "dev_server.py").read_text(
        encoding="utf-8"
    )
    assert '"TZ": os.environ.get("TZ") or "UTC"' in source


def test_the_warning_only_shows_with_no_configuration(tmp_path):
    # nothing set and nothing detectable: the fallback warns
    fallback = Settings.from_env(
        base_env(tmp_path / "fallback"),
        timezone_file=tmp_path / "no-tz",
        localtime_file=tmp_path / "no-localtime",
    )
    assert (fallback.tz, fallback.tz_source) == ("UTC", "default")
    assert "tz_utc" in admin_warnings(fallback, FakeAdGuard())

    # an explicit TZ never warns, even when it is UTC
    explicit = Settings.from_env(
        base_env(tmp_path / "explicit", TZ="UTC"),
        timezone_file=tmp_path / "no-tz",
        localtime_file=tmp_path / "no-localtime",
    )
    assert "tz_utc" not in admin_warnings(explicit, FakeAdGuard())

    # a detected system timezone never warns, even when the system says UTC
    timezone = tmp_path / "system-timezone"
    timezone.write_text("Etc/UTC\n")
    system = Settings.from_env(
        base_env(tmp_path / "system"),
        timezone_file=timezone,
        localtime_file=tmp_path / "no-localtime",
    )
    assert (system.tz, system.tz_source) == ("Etc/UTC", "system")
    assert "tz_utc" not in admin_warnings(system, FakeAdGuard())

    # a non-UTC zone never warns
    berlin = Settings.from_env(
        base_env(tmp_path / "berlin", TZ="Europe/Berlin"),
        timezone_file=tmp_path / "no-tz",
        localtime_file=tmp_path / "no-localtime",
    )
    assert "tz_utc" not in admin_warnings(berlin, FakeAdGuard())


def test_the_warning_text_describes_the_fallback():
    text = i18n.t("warn.tz_utc")
    assert "No timezone is configured" in text
    assert "Europe/London" in text


def test_startup_logs_where_the_timezone_came_from(tmp_path, caplog):
    fallback = Settings.from_env(
        base_env(tmp_path / "log-fallback"),
        timezone_file=tmp_path / "no-tz",
        localtime_file=tmp_path / "no-localtime",
    )
    with (
        caplog.at_level("INFO", logger="timefence.web"),
        TestClient(create_app(fallback, adguard=FakeAdGuard())),
    ):
        pass
    assert "Timezone: UTC (from default)" in caplog.text

    caplog.clear()
    berlin = Settings.from_env(
        base_env(tmp_path / "log-env", TZ="Europe/Berlin", LOG_LEVEL="INFO"),
    )
    with (
        caplog.at_level("INFO", logger="timefence.web"),
        TestClient(create_app(berlin, adguard=FakeAdGuard())),
    ):
        pass
    assert "Timezone: Europe/Berlin (from TZ)" in caplog.text
