"""Config validation."""

from __future__ import annotations

import os
import stat

import pytest

from timefence.config import ConfigError, Settings, normalize_base_path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", "/"), ("/", "/"), ("timer", "/timer"), ("/timer/", "/timer"), ("a/b/", "/a/b")],
)
def test_normalize_base_path(raw, expected):
    assert normalize_base_path(raw) == expected


def _env(**over):
    env = {
        "ADGUARD_PASSWORD": "secret",
        "DATA_DIR": "/tmp/timefence-test-data",
    }
    env.update(over)
    return env


def test_defaults_are_documented_values():
    settings = Settings.from_env(_env())
    assert settings.listen_host == "127.0.0.1"
    assert settings.listen_port == 8099
    assert settings.base_path == "/"
    assert settings.tz == "UTC"
    assert settings.reconcile_interval == 30
    assert settings.secure_cookies is True


def test_my_instance_style_settings():
    settings = Settings.from_env(
        _env(
            BASE_PATH="timer", TZ="Asia/Kolkata", LISTEN_PORT="8099", TRUSTED_PROXIES="127.0.0.1/32"
        )
    )
    assert settings.base_path == "/timer"
    assert settings.zone.key == "Asia/Kolkata"
    assert str(settings.trusted_proxies[0]) == "127.0.0.1/32"


def test_password_file_is_read(tmp_path):
    path = tmp_path / "pass"
    path.write_text("from-file\n")
    settings = Settings.from_env(_env(ADGUARD_PASSWORD="", ADGUARD_PASSWORD_FILE=str(path)))
    assert settings.adguard_password == "from-file"
    assert "***" in str(settings.redacted()["adguard_password"])


def test_missing_password_is_rejected_at_validation():
    settings = Settings.from_env(_env(ADGUARD_PASSWORD=""))
    with pytest.raises(ConfigError):
        settings.validate()


def test_bad_timezone():
    with pytest.raises(ConfigError):
        Settings.from_env(_env(TZ="Mars/Olympus"))


def test_bad_port():
    settings = Settings.from_env(_env(LISTEN_PORT="99999"))
    with pytest.raises(ConfigError):
        settings.validate()


def test_bad_trusted_proxy():
    with pytest.raises(ConfigError):
        Settings.from_env(_env(TRUSTED_PROXIES="not-a-cidr"))


def test_reconcile_interval_floor():
    settings = Settings.from_env(_env(RECONCILE_INTERVAL="1"))
    with pytest.raises(ConfigError):
        settings.validate()


def test_bad_adguard_url():
    settings = Settings.from_env(_env(ADGUARD_URL="ftp://x"))
    with pytest.raises(ConfigError):
        settings.validate()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
def test_data_dir_is_private(tmp_path):
    target = tmp_path / "private"
    settings = Settings.from_env({"ADGUARD_PASSWORD": "secret", "DATA_DIR": str(target)})
    settings.validate()
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o700
