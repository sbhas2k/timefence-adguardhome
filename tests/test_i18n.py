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
