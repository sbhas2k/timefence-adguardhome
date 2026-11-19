"""Proofs for install.sh and its env-file writer.

install.sh itself needs root, systemd and a real pip, so its static checks read
the text; the quoting rules live in deploy/write-env.sh, which is sourced here
and run for real (any POSIX shell, no root).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from timefence.__main__ import _read_env_file

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
WRITER = ROOT / "deploy" / "write-env.sh"
BASH = shutil.which("bash")

TRICKY = "pa ss$HOME`id`'quote\"back\\slash=semi;colon"


def write_env(tmp_path: Path, *, tz: str | None) -> Path:
    dest = tmp_path / "timefence.env"
    lines = [
        f'. "{WRITER.as_posix()}"',
        "ADGUARD_URL='http://adguard.example:3000/?a=b'",
        "ADGUARD_USER='admin'",
        "ADGUARD_PASSWORD=$TF_PASSWORD",
        "ADGUARD_PASSWORD_FILE=''",
        "BASE_PATH='/timer'",
        "LISTEN_HOST='127.0.0.1'",
        "LISTEN_PORT='8099'",
        "DATA_DIR='/var/lib/timefence'",
        "RECONCILE_INTERVAL='30'",
        "TRUSTED_PROXIES=''",
        "SECURE_COOKIES='true'",
        "LOG_LEVEL='INFO'",
        f'tf_write_env_file "{dest.as_posix()}"',
    ]
    if tz is not None:
        lines.insert(5, f"TZ='{tz}'")
    script = tmp_path / "run.sh"
    script.write_text("set -eu\n" + "\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    env = {"PATH": "/usr/bin:/bin", "TF_PASSWORD": TRICKY}
    result = subprocess.run(
        [BASH, str(script)], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    return dest


def test_a_tricky_password_round_trips_through_the_reader(tmp_path: Path) -> None:
    if BASH is None:
        pytest.skip("needs a POSIX shell (bash)")
    dest = write_env(tmp_path, tz="Asia/Kolkata")
    text = dest.read_text(encoding="utf-8")
    assert "ADGUARD_PASSWORD='" in text
    parsed = _read_env_file(dest)
    assert parsed["ADGUARD_PASSWORD"] == TRICKY
    assert parsed["ADGUARD_URL"] == "http://adguard.example:3000/?a=b"
    assert parsed["BASE_PATH"] == "/timer"
    assert parsed["TZ"] == "Asia/Kolkata"
    assert json.dumps(parsed["ADGUARD_PASSWORD"])  # a plain str, not evaluated shell


def test_tz_is_only_written_when_the_caller_set_it(tmp_path: Path) -> None:
    if BASH is None:
        pytest.skip("needs a POSIX shell (bash)")
    dest = write_env(tmp_path, tz=None)
    text = dest.read_text(encoding="utf-8")
    assert "# TZ=\n" in text
    assert "\nTZ=" not in text
    assert "TZ" not in _read_env_file(dest)


def test_the_installer_parses_the_env_file_instead_of_sourcing_it() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert '. "${ENV_FILE}"' not in text
    assert "set -a" not in text
    assert "tf_write_env_file" in text
    assert "--require-hashes" in text
    assert "--no-deps" in text
    assert '"http://${LISTEN_HOST}:${LISTEN_PORT}${BASE_PATH%/}/healthz"' in text
    assert "PIN" not in text
