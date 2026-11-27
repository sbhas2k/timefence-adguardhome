"""The GitHub workflows: every input belongs to the action that owns it.

Commit f37f8db swapped some `uses:` lines, which left each action's `with:`
block hanging under the neighbouring action. GitHub only warns about the
unknown inputs, so the job silently lost its Python and its CodeQL settings.
These checks are plain text parsing of the YAML with the standard library.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

STEP = re.compile(r"^(?P<indent>[ ]*)-[ ]+(?P<first>\S.*)$")
JOB = re.compile(r"^  (?P<name>[A-Za-z0-9_-]+):[ ]*$")
USES = re.compile(r"^uses:[ ]*(?P<action>[^\s#]+)")
INPUT = re.compile(r"^(?P<key>[A-Za-z0-9_-]+):[ ]*(?P<value>[^#]*?)[ ]*$")
PINNED = re.compile(r"[\"']?ruff==(?P<version>\d+(?:\.\d+)+)[\"']?")

# the inputs each action reads; a `with:` on any other step is a bug
OWNED_INPUTS = {
    "python-version": "actions/setup-python",
    "cache": "actions/setup-python",
    "languages": "github/codeql-action/init",
    "config-file": "github/codeql-action/init",
}


def workflow_files() -> list[Path]:
    assert WORKFLOWS, "no workflows found"
    return WORKFLOWS


def parse_steps(path: Path) -> list[dict]:
    """Every step of every job: {job, action, inputs, order}."""
    lines = path.read_text(encoding="utf-8").splitlines()
    steps: list[dict] = []
    job = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        job_match = JOB.match(line)
        if job_match:
            job = job_match.group("name")
            index += 1
            continue
        step_match = STEP.match(line)
        if not step_match:
            index += 1
            continue
        indent = len(step_match.group("indent"))
        block = [step_match.group("first")]
        cursor = index + 1
        while cursor < len(lines):
            follower = lines[cursor]
            if not follower.strip():
                block.append("")
                cursor += 1
                continue
            if len(follower) - len(follower.lstrip(" ")) <= indent:
                break
            block.append(follower)
            cursor += 1
        steps.append(parse_step(block, indent, job, len(steps)))
        index = cursor
    return steps


def parse_step(block: list[str], indent: int, job: str, order: int) -> dict:
    # `- name: ...` steps name their action on the next line, at indent + 2
    direct = indent + 2
    action = None
    uses = USES.match(block[0].strip())
    if uses:
        action = uses.group("action")
    for line in block[1:]:
        if not line.strip() or len(line) - len(line.lstrip(" ")) > direct:
            continue
        uses = USES.match(line.strip())
        if uses:
            action = uses.group("action")
            break
    inputs: dict[str, str] = {}
    inside_with = False
    with_indent = None
    for line in block[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        line_indent = len(line) - len(line.lstrip(" "))
        if stripped == "with:":
            inside_with = True
            with_indent = line_indent
            continue
        if inside_with and with_indent is not None and line_indent <= with_indent:
            inside_with = False
        if not inside_with:
            continue
        input_match = INPUT.match(stripped)
        if input_match:
            inputs[input_match.group("key")] = input_match.group("value").strip()
    return {
        "job": job,
        "action": action,
        "inputs": inputs,
        "order": order,
    }


def owner_of(key: str, value: str) -> str | None:
    if key == "cache" and value.strip("\"'") != "pip":
        return None
    return OWNED_INPUTS.get(key)


def test_every_python_input_belongs_to_setup_python():
    for path in workflow_files():
        for step in parse_steps(path):
            for key, value in step["inputs"].items():
                owner = owner_of(key, value)
                if owner is None:
                    continue
                action = step["action"] or ""
                assert action.startswith(owner + "@"), (
                    f"{path.name}: '{key}' sits on '{action or step['job']}' "
                    f"in job '{step['job']}' but belongs to {owner}"
                )


def test_every_setup_python_step_sets_the_version():
    found = 0
    for path in workflow_files():
        for step in parse_steps(path):
            if not (step["action"] or "").startswith("actions/setup-python@"):
                continue
            found += 1
            assert "python-version" in step["inputs"], (
                f"{path.name}: the setup-python step in job '{step['job']}' has no python-version"
            )
    assert found, "no actions/setup-python step in any workflow"


def test_no_input_is_missed_by_the_parser():
    """The count in the raw text matches the count the parser attributed."""
    for path in workflow_files():
        text = path.read_text(encoding="utf-8")
        steps = parse_steps(path)
        for key in OWNED_INPUTS:
            in_text = len(re.findall(rf"^\s+{key}:", text, flags=re.MULTILINE))
            parsed = sum(1 for step in steps if key in step["inputs"])
            assert in_text == parsed, f"{path.name}: '{key}' x{in_text} in text, x{parsed} parsed"


def test_checkout_runs_before_setup_python():
    for path in workflow_files():
        jobs: dict[str, list[dict]] = {}
        for step in parse_steps(path):
            jobs.setdefault(step["job"], []).append(step)
        for job, steps in jobs.items():
            actions = [step["action"] or "" for step in steps]
            if not any(a.startswith("actions/setup-python@") for a in actions):
                continue
            checkout = [i for i, a in enumerate(actions) if a.startswith("actions/checkout@")]
            setup = [i for i, a in enumerate(actions) if a.startswith("actions/setup-python@")]
            assert checkout, f"{path.name}: job '{job}' has no checkout"
            assert checkout[0] < setup[0], (
                f"{path.name}: job '{job}' sets up Python before checking out"
            )


def test_codeql_scans_python_and_the_hand_written_javascript():
    """The static UI is hand-written JS, so both languages stay in scope."""
    inits = [
        step
        for path in workflow_files()
        for step in parse_steps(path)
        if (step["action"] or "").startswith("github/codeql-action/init@")
    ]
    assert inits, "no github/codeql-action/init step in any workflow"
    for step in inits:
        languages = {
            language.strip() for language in step["inputs"].get("languages", "").split(",")
        }
        assert {"python", "javascript"} <= languages, (
            f"CodeQL init in job '{step['job']}' scans {sorted(languages)} instead of "
            "python and javascript"
        )


def test_the_workflows_lint_with_the_pinned_ruff():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    pinned = set(PINNED.findall(pyproject))
    assert len(pinned) == 1, f"pyproject.toml pins {pinned}"
    for path in workflow_files():
        installed = set(PINNED.findall(path.read_text(encoding="utf-8")))
        for version in installed:
            assert version in pinned, (
                f"{path.name}: installs ruff {version}, pyproject.toml pins {pinned.pop()}"
            )
