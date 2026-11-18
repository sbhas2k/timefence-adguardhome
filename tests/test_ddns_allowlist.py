"""Proofs for deploy/ddns-allowlist.sh (remote VM, port 53 open only to home).

The script runs `getent` and `ufw`, so every test puts a directory of stub
executables first on PATH and lets them record the arguments they were called
with. Nothing here needs root or a real firewall.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "ddns-allowlist.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    BASH is None, reason="needs a POSIX shell (bash) to execute the helper"
)

ALLOW_UDP = "ufw allow from 203.0.113.9 to any port 53 proto udp comment timefence-ddns"
ALLOW_TCP = "ufw allow from 203.0.113.9 to any port 53 proto tcp comment timefence-ddns"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8", newline="\n")
    path.chmod(0o755)


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        ip: str = "203.0.113.9",
        getent_exit: int = 0,
        ufw_exit: int = 0,
    ) -> None:
        self.tmp = tmp_path
        self.calls_file = tmp_path / "calls.log"
        self.calls_file.write_text("", encoding="utf-8")
        self.state_dir = tmp_path / "state"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _stub(
            bin_dir / "getent",
            'printf "getent %s\\n" "$*" >>"$TF_CALLS"\n'
            'if [ "$TF_GETENT_EXIT" -ne 0 ]; then exit "$TF_GETENT_EXIT"; fi\n'
            'printf "%s      STREAM ddns.example.org\\n" "$TF_GETENT_IP"\n',
        )
        _stub(
            bin_dir / "ufw",
            'printf "ufw %s\\n" "$*" >>"$TF_CALLS"\n'
            'if [ "$TF_UFW_EXIT" -ne 0 ]; then exit "$TF_UFW_EXIT"; fi\n',
        )
        self.bin_dir = bin_dir
        self.ip = ip
        self.getent_exit = getent_exit
        self.ufw_exit = ufw_exit

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PATH"] = str(self.bin_dir.as_posix()) + os.pathsep + env.get("PATH", "")
        env["TF_CALLS"] = self.calls_file.as_posix()
        env["TF_GETENT_IP"] = self.ip
        env["TF_GETENT_EXIT"] = str(self.getent_exit)
        env["TF_UFW_EXIT"] = str(self.ufw_exit)
        return subprocess.run(
            [BASH or "bash", SCRIPT.as_posix(), *args],
            cwd=str(self.tmp),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def raw_calls(self) -> list[str]:
        text = self.calls_file.read_text(encoding="utf-8")
        return [line for line in text.splitlines() if line]

    def ufw_calls(self) -> list[str]:
        return [line for line in self.raw_calls() if line.startswith("ufw ")]

    def state(self) -> str:
        return (self.state_dir / "ip").read_text(encoding="utf-8")

    def set_state(self, address: str) -> None:
        self.state_dir.mkdir(exist_ok=True)
        (self.state_dir / "ip").write_text(address + "\n", encoding="utf-8")


def test_hostname_with_shell_metacharacters_is_rejected(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    for bad in ("ddns.example.org; ls", "ddns example.org", "ddns.example.org&&id"):
        proc = harness.run(bad, "--state-dir", harness.state_dir.as_posix())
        assert proc.returncode == 64, (bad, proc.returncode, proc.stderr)
    assert harness.raw_calls() == []


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.1.2.3", "192.168.1.5", "100.64.0.9", "0.0.0.0"]
)
def test_non_public_address_is_refused(tmp_path: Path, address: str) -> None:
    harness = Harness(tmp_path, ip=address)
    harness.set_state("198.51.100.7")
    proc = harness.run("ddns.example.org", "--state-dir", harness.state_dir.as_posix())
    assert proc.returncode == 2, (address, proc.stderr)
    assert harness.ufw_calls() == []
    assert harness.state() == "198.51.100.7\n"


def test_first_run_allows_both_protocols_and_records_the_address(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    proc = harness.run("ddns.example.org", "--state-dir", harness.state_dir.as_posix())
    assert proc.returncode == 0, proc.stderr
    assert harness.ufw_calls() == [ALLOW_UDP, ALLOW_TCP]
    assert harness.state() == "203.0.113.9\n"


def test_same_address_makes_no_firewall_call(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.set_state("203.0.113.9")
    proc = harness.run("ddns.example.org", "--state-dir", harness.state_dir.as_posix())
    assert proc.returncode == 0, proc.stderr
    assert harness.ufw_calls() == []
    assert "no firewall change" in proc.stderr


def test_new_address_is_added_before_the_old_one_is_removed(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ip="203.0.113.10")
    harness.set_state("203.0.113.9")
    proc = harness.run("ddns.example.org", "--state-dir", harness.state_dir.as_posix())
    assert proc.returncode == 0, proc.stderr
    calls = harness.ufw_calls()
    assert calls == [
        "ufw allow from 203.0.113.10 to any port 53 proto udp comment timefence-ddns",
        "ufw allow from 203.0.113.10 to any port 53 proto tcp comment timefence-ddns",
        "ufw delete allow from 203.0.113.9 to any port 53 proto udp comment timefence-ddns",
        "ufw delete allow from 203.0.113.9 to any port 53 proto tcp comment timefence-ddns",
    ]
    assert harness.state() == "203.0.113.10\n"


def test_resolution_failure_keeps_rules_and_state(tmp_path: Path) -> None:
    harness = Harness(tmp_path, getent_exit=3)
    harness.set_state("198.51.100.7")
    proc = harness.run("ddns.example.org", "--state-dir", harness.state_dir.as_posix())
    assert proc.returncode == 1, proc.stderr
    assert harness.ufw_calls() == []
    assert harness.state() == "198.51.100.7\n"


def test_failed_add_keeps_the_old_rule_and_state(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ip="203.0.113.10", ufw_exit=1)
    harness.set_state("203.0.113.9")
    proc = harness.run("ddns.example.org", "--state-dir", harness.state_dir.as_posix())
    assert proc.returncode != 0
    assert harness.state() == "203.0.113.9\n"
    assert not any("delete" in call for call in harness.ufw_calls())


def test_dry_run_prints_commands_and_changes_nothing(tmp_path: Path) -> None:
    harness = Harness(tmp_path, ip="203.0.113.10")
    harness.set_state("203.0.113.9")
    proc = harness.run("ddns.example.org", "--state-dir", harness.state_dir.as_posix(), "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "would run: ufw allow from 203.0.113.10" in proc.stderr
    assert "would run: ufw delete allow from 203.0.113.9" in proc.stderr
    assert harness.ufw_calls() == []
    assert harness.state() == "203.0.113.9\n"

    missing = tmp_path / "never-created"
    proc = harness.run("ddns.example.org", "--state-dir", missing.as_posix(), "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert not missing.exists()


def test_script_never_opens_dns_to_the_world() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "COMMENT='timefence-ddns'" in text
    for line in text.splitlines():
        lowered = line.lower()
        for forbidden in ("ufw reset", "ufw disable", "default allow"):
            assert forbidden not in lowered, line
        if "ufw" in lowered and "allow" in lowered:
            assert 'from "$ip"' in line or 'from "$old_ip"' in line, line
