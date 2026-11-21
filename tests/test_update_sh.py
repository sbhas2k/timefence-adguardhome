"""Proofs for deploy/update.sh (in-place update of a host checkout).

The script runs `git`, `docker`, `curl`, `sudo` and `sleep`, so every test puts a
directory of stub executables first on PATH and lets them record what they were
called with. The temp REPO_DIR holds a real copy of pyproject.toml and a small
compose file, so no git repository, daemon or root is needed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "update.sh"
BASH = shutil.which("bash")
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
PENDING = "0.9.0"

COMPOSE = f"""\
services:
  timefence:
    build: .
    image: timefence:{PENDING}
    container_name: timefence
"""

GIT_STUB = """\
printf 'git %s\\n' "$*" >>"$TF_CALLS"
if [ "${TF_GIT_PULL_EXIT:-0}" -ne 0 ]; then
  echo "fatal: Not possible to fast-forward, aborting." >&2
  exit "$TF_GIT_PULL_EXIT"
fi
"""

DOCKER_STUB = """\
printf 'docker %s\\n' "$*" >>"$TF_CALLS"
if [ "${TF_DOCKER_EXIT:-0}" -ne 0 ]; then
  exit "$TF_DOCKER_EXIT"
fi
"""

CURL_STUB = """\
printf 'curl %s\\n' "$*" >>"$TF_CALLS"
codes=(${TF_HEALTH_CODES})
count=0
if [ -f "$TF_HEALTH_INDEX" ]; then
  count="$(<"$TF_HEALTH_INDEX")"
fi
code=200
if [ "$count" -lt "${#codes[@]}" ]; then
  code="${codes[$count]}"
fi
printf '%s\\n' "$((count + 1))" >"$TF_HEALTH_INDEX"
printf '%s' "$code"
"""

SUDO_STUB = """\
printf 'sudo %s\\n' "$*" >>"$TF_CALLS"
exec "$@"
"""

SLEEP_STUB = """\
printf 'sleep %s\\n' "$*" >>"$TF_CALLS"
"""

# The health loop repeats `seq 1 40` times; a real retry count costs a minute of
# process spawns on Windows, so the stub shortens it where the count is not the
# point (TF_SEQ_MAX).
SEQ_STUB = """\
printf 'seq %s\\n' "$*" >>"$TF_CALLS"
i=1
while [ "$i" -le "${TF_SEQ_MAX:-40}" ]; do
  printf '%s\\n' "$i"
  i=$((i + 1))
done
"""


@pytest.fixture()
def shell() -> str:
    """The shell that runs the helper; the static checks do not need one."""
    if BASH is None:
        pytest.skip("needs a POSIX shell (bash) to execute the helper")
    return BASH


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8", newline="\n")
    path.chmod(0o755)


class Harness:
    def __init__(self, shell: str, tmp_path: Path) -> None:
        self.shell = shell
        self.tmp = tmp_path
        self.calls_file = tmp_path / "calls.log"
        self.calls_file.write_text("", encoding="utf-8")
        self.health = ""
        self.seq_max = 40

        self.bin_dir = tmp_path / "bin"
        self.bin_dir.mkdir()
        _stub(self.bin_dir / "git", GIT_STUB)
        _stub(self.bin_dir / "docker", DOCKER_STUB)
        _stub(self.bin_dir / "curl", CURL_STUB)
        _stub(self.bin_dir / "sudo", SUDO_STUB)
        _stub(self.bin_dir / "sleep", SLEEP_STUB)
        _stub(self.bin_dir / "seq", SEQ_STUB)

        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        shutil.copyfile(ROOT / "pyproject.toml", self.repo / "pyproject.toml")
        self.compose = self.repo / "docker-compose.yml"
        self.compose.write_text(COMPOSE, encoding="utf-8", newline="\n")

        self.git_pull_exit = 0
        self.docker_exit = 0

    def run(self, *args: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        for name in ("COMPOSE_FILE", "HEALTH_URL", "BASE_PATH", "LISTEN_PORT", "REPO_DIR"):
            env.pop(name, None)
        env["PATH"] = str(self.bin_dir.as_posix()) + os.pathsep + env.get("PATH", "")
        env["TF_CALLS"] = self.calls_file.as_posix()
        env["TF_HEALTH_CODES"] = self.health
        env["TF_HEALTH_INDEX"] = (self.tmp / "health.idx").as_posix()
        env["TF_GIT_PULL_EXIT"] = str(self.git_pull_exit)
        env["TF_DOCKER_EXIT"] = str(self.docker_exit)
        env["TF_SEQ_MAX"] = str(self.seq_max)
        env["REPO_DIR"] = self.repo.as_posix()
        env.update(overrides)
        return subprocess.run(
            [self.shell, SCRIPT.as_posix(), *args],
            cwd=str(self.tmp),
            env=env,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )

    def raw_calls(self) -> list[str]:
        text = self.calls_file.read_text(encoding="utf-8")
        return [line for line in text.splitlines() if line]

    def docker_calls(self) -> list[str]:
        return [line for line in self.raw_calls() if line.startswith("docker ")]

    def compose_calls(self) -> list[str]:
        return [line for line in self.raw_calls() if line.startswith("docker compose ")]

    def curl_calls(self) -> list[str]:
        return [line for line in self.raw_calls() if line.startswith("curl ")]

    def sudo_calls(self) -> list[str]:
        return [line for line in self.raw_calls() if line.startswith("sudo ")]

    def tag(self) -> str | None:
        text = self.compose.read_text(encoding="utf-8")
        found = re.search(r"^[ \t]*image: timefence:(\S+)", text, re.MULTILINE)
        return found.group(1) if found else None


def test_the_default_compose_file_is_the_one_in_the_checkout() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "/opt/" not in text, "no hardcoded host path"
    assert "$REPO_DIR/docker-compose.yml" in text


def test_update_pulls_builds_switches_and_reports_the_rollback(shell: str, tmp_path: Path) -> None:
    harness = Harness(shell, tmp_path)
    harness.health = "503 503 200"

    proc = harness.run()

    assert proc.returncode == 0, proc.stderr
    assert harness.tag() == VERSION
    calls = harness.raw_calls()
    assert "git pull --ff-only" in calls
    assert f"docker build -t timefence:{VERSION} ." in calls
    assert f"docker compose -f {harness.compose.as_posix()} up -d" in calls
    assert calls.index("git pull --ff-only") < calls.index(f"docker build -t timefence:{VERSION} .")
    assert f"--rollback {PENDING}" in proc.stdout
    assert harness.sudo_calls() == []


@pytest.mark.parametrize(
    ("overrides", "url"),
    [
        ({}, "http://127.0.0.1:8099/healthz"),
        ({"BASE_PATH": "/"}, "http://127.0.0.1:8099/healthz"),
        ({"BASE_PATH": "/timer"}, "http://127.0.0.1:8099/timer/healthz"),
        ({"BASE_PATH": "/timer/"}, "http://127.0.0.1:8099/timer/healthz"),
        ({"LISTEN_PORT": "9000"}, "http://127.0.0.1:9000/healthz"),
        ({"LISTEN_PORT": "9000", "BASE_PATH": "/timer"}, "http://127.0.0.1:9000/timer/healthz"),
    ],
)
def test_the_health_url_is_derived_from_base_path_and_port(
    shell: str, tmp_path: Path, overrides: dict[str, str], url: str
) -> None:
    harness = Harness(shell, tmp_path)

    proc = harness.run(**overrides)

    assert proc.returncode == 0, proc.stderr
    assert harness.curl_calls(), "health was never checked"
    assert all(line.endswith(url) for line in harness.curl_calls()), harness.curl_calls()


def test_health_url_overrides_the_derivation(shell: str, tmp_path: Path) -> None:
    harness = Harness(shell, tmp_path)

    proc = harness.run(HEALTH_URL="http://127.0.0.1:1234/healthz", BASE_PATH="/timer")

    assert proc.returncode == 0, proc.stderr
    assert harness.curl_calls()
    assert all(line.endswith("http://127.0.0.1:1234/healthz") for line in harness.curl_calls())


def test_a_failed_pull_changes_nothing_and_says_so(shell: str, tmp_path: Path) -> None:
    harness = Harness(shell, tmp_path)
    harness.git_pull_exit = 1

    proc = harness.run()

    assert proc.returncode != 0
    assert harness.tag() == PENDING
    assert harness.docker_calls() == []
    assert proc.stderr.count("git pull --ff-only failed") == 1
    message = [line for line in proc.stderr.splitlines() if "git pull --ff-only failed" in line]
    assert len(message) == 1


def test_a_health_check_that_never_passes_keeps_the_new_tag_and_fails(
    shell: str, tmp_path: Path
) -> None:
    harness = Harness(shell, tmp_path)
    harness.health = "503 " * 60
    harness.seq_max = 2

    proc = harness.run()

    assert proc.returncode == 1
    assert harness.tag() == VERSION
    assert f"--rollback {PENDING}" in proc.stderr
    assert len(harness.curl_calls()) > 1


def test_rollback_rewrites_the_tag_restarts_and_checks_health(shell: str, tmp_path: Path) -> None:
    harness = Harness(shell, tmp_path)

    proc = harness.run("--rollback", "0.8.0")

    assert proc.returncode == 0, proc.stderr
    assert harness.tag() == "0.8.0"
    assert harness.compose_calls() == [f"docker compose -f {harness.compose.as_posix()} up -d"]
    assert [line for line in harness.raw_calls() if line.startswith("git ")] == []
    assert harness.curl_calls()


def test_rollback_still_works_with_the_old_word_form(shell: str, tmp_path: Path) -> None:
    harness = Harness(shell, tmp_path)

    proc = harness.run("rollback", "0.8.0")

    assert proc.returncode == 0, proc.stderr
    assert harness.tag() == "0.8.0"


def test_rollback_that_stays_unhealthy_fails_with_the_new_tag_in_place(
    shell: str, tmp_path: Path
) -> None:
    harness = Harness(shell, tmp_path)
    harness.health = "503 " * 60
    harness.seq_max = 2

    proc = harness.run("--rollback", "0.8.0")

    assert proc.returncode == 1
    assert harness.tag() == "0.8.0"


def test_rollback_needs_a_tag(shell: str, tmp_path: Path) -> None:
    harness = Harness(shell, tmp_path)

    proc = harness.run("--rollback")

    assert proc.returncode == 2
    assert proc.stderr.strip()
    assert harness.raw_calls() == []
    assert harness.tag() == PENDING


@pytest.mark.parametrize(
    "tag", ["1.0.0|s|x|y|", "../1.0.0", "1.0.0;id", "1 0", "$(id)", "a/b", "1.0.0.*", ""]
)
def test_a_tag_with_shell_or_sed_metacharacters_is_refused(
    shell: str, tmp_path: Path, tag: str
) -> None:
    harness = Harness(shell, tmp_path)

    proc = harness.run("--rollback", tag)

    assert proc.returncode == 2, (tag, proc.stderr)
    assert harness.raw_calls() == []
    assert harness.tag() == PENDING


def test_a_missing_compose_file_is_an_error(shell: str, tmp_path: Path) -> None:
    harness = Harness(shell, tmp_path)
    harness.compose.unlink()

    proc = harness.run()

    assert proc.returncode == 1
    assert "compose file not found" in proc.stderr
    assert harness.raw_calls() == []


def test_a_missing_compose_file_is_an_error_for_rollback(shell: str, tmp_path: Path) -> None:
    harness = Harness(shell, tmp_path)
    harness.compose.unlink()

    proc = harness.run("--rollback", "0.8.0")

    assert proc.returncode == 1
    assert "compose file not found" in proc.stderr
    assert harness.raw_calls() == []


def test_a_failed_build_leaves_the_compose_file_alone(shell: str, tmp_path: Path) -> None:
    harness = Harness(shell, tmp_path)
    harness.docker_exit = 1

    proc = harness.run()

    assert proc.returncode != 0
    assert harness.tag() == PENDING
    assert harness.compose_calls() == []
