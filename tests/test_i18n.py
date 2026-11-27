"""Every string key a template or script asks for must exist."""

from __future__ import annotations

import re
from pathlib import Path

from timefence import i18n

PACKAGE = Path(i18n.__file__).parent
TEMPLATES = PACKAGE / "templates"
STATIC = PACKAGE / "static"
CALL = re.compile(r"""\bt\(\s*["']([^"']+)["']""")


def test_i18n_keys_used_by_templates_and_scripts_exist():
    missing: list[tuple[str, str]] = []
    paths = sorted(TEMPLATES.glob("*.html")) + sorted(STATIC.glob("*.js"))
    assert paths
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for key in CALL.findall(text):
            if key.endswith("."):
                continue  # a prefix the code completes at runtime, not a key
            if key not in i18n.STRINGS["en"]:
                missing.append((path.name, key))
    assert missing == []


def test_group_validation_error_keys_exist():
    """Every validation sentence the group editor can show lives in i18n."""
    required = {
        "error.invalid_domain",
        "error.invalid_owner",
        "error.duplicate_name",
        "error.window_start_equals_end",
        "error.window_bad_time",
        "error.window_bad_days",
        "error.window_too_many",
        "error.name_not_text",
        "error.paused_not_bool",
        "error.group_domains_text",
    }
    assert required <= set(i18n.STRINGS["en"])


def test_i18n_keys_used_by_the_server_exist():
    source = (PACKAGE / "api.py").read_text(encoding="utf-8")
    keys = set(re.findall(r"""i18n\.t\(\s*["']([^"']+)["']""", source))
    assert keys
    missing = sorted(key for key in keys if key not in i18n.STRINGS["en"])
    assert missing == []


def test_alert_messages_come_from_i18n():
    required = {
        "alert.closed",
        "alert.closed_day",
        "alert.sync_failing",
        "alert.sync_recovered",
        "alert.test",
    }
    assert required <= set(i18n.STRINGS["en"])
    from timefence import alerts

    text = Path(alerts.__file__).read_text(encoding="utf-8")
    matches = list(re.finditer(r'"alert\.[a-z_]+"', text))
    assert matches
    for match in matches:
        before = text[max(0, match.start() - len("i18n.t(")) : match.start()]
        assert before == "i18n.t(", match.group(0)


def test_every_warning_code_emitted_by_the_server_has_a_string():
    codes: set[str] = set()
    for name in ("reconciler.py", "api.py"):
        source = (PACKAGE / name).read_text(encoding="utf-8")
        codes.update(re.findall(r'warnings\.add\("([a-z0-9_]+)"\)', source))
        codes.update(re.findall(r'warnings\.discard\("([a-z0-9_]+)"\)', source))
        codes.update(re.findall(r'"([a-z0-9_]+)"\s*\}\s*if settings\.tz_source', source))
    assert "rules_section_damaged" in codes
    missing = sorted(code for code in codes if f"warn.{code}" not in i18n.STRINGS["en"])
    assert missing == []
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 't("warn." + code)' in app_js
