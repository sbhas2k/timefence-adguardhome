"""Static checks for the mobile and accessibility polish.

These read the shipped CSS, templates and scripts. They cannot measure a
rendered tap target or a wrapped line; the browser pre-check is the
authoritative evidence. They do fail if the relevant rule, branch or string is
removed, so a regression is caught in CI as well as by eye.
"""

from __future__ import annotations

import re
from pathlib import Path

from timefence import i18n


def package_file(*parts: str) -> Path:
    from timefence import api

    return Path(api.__file__).parent.joinpath(*parts)


def read(*parts: str) -> str:
    return package_file(*parts).read_text(encoding="utf-8")


def rule(css: str, selector: str) -> str:
    match = re.search(r"(?m)^\s*" + re.escape(selector) + r"\s*\{", css)
    assert match, selector
    end = css.index("}", match.end())
    return css[match.start() : end]


def css_text() -> str:
    return read("static", "style.css")


RESULT_KEYS = (
    "alerts.result_ok",
    "alerts.result_failed",
    "alerts.reason_connect",
    "alerts.reason_timeout",
    "alerts.reason_http",
    "alerts.reason_config",
    "alerts.reason_limited",
    "alerts.reason_other",
)


def test_the_header_stays_on_one_line():
    css = css_text()
    html = read("templates", "base.html")
    assert "white-space: nowrap" in rule(css, ".topbar .btn")
    assert 'class="brand-full"' in html
    assert 'class="brand-short"' in html
    assert "display: none" in rule(css, ".brand-short")
    mobile = css[css.index("@media (max-width: 480px)") :]
    assert "display: none" in rule(mobile, ".brand-full")
    assert "display: inline" in rule(mobile, ".brand-short")
    short = i18n.STRINGS["en"]["app.header_short"]
    assert short
    assert len(short) < len(i18n.STRINGS["en"]["app.title"])


def test_tap_targets_are_at_least_44_pixels():
    css = css_text()
    assert "min-width: 44px" in rule(css, ".btn")
    assert "min-height: 44px" in rule(css, ".check")
    assert "min-width: 20px" in rule(css, ".check input[type='checkbox']")
    mobile = css[css.index("@media (max-width: 480px)") :]
    assert "min-width: 44px" in rule(mobile, ".chip button")
    assert "min-height: 44px" in rule(mobile, ".chip button")

    html = read("templates", "app.html")
    assert html.count('class="check"') >= 8  # show all, alerts enabled, six events
    js = read("static", "app.js")
    assert '<label class="check"><input type="checkbox" id="g-paused"' in js


def test_settings_forms_have_distinct_save_names():
    html = read("templates", "app.html")
    assert 't("account.save")' in html
    assert 't("alerts.save")' in html
    assert "common.save" not in html
    assert i18n.STRINGS["en"]["account.save"] != i18n.STRINGS["en"]["alerts.save"]


def test_the_alert_result_line_maps_categories():
    js = read("static", "app.js")
    for key in RESULT_KEYS:
        assert key in js, key
        assert key in i18n.STRINGS["en"], key
    # the API field is read, but the internal category word is never printed
    assert "last_result" in js
    assert "{ result:" not in js
    assert "alerts.last_result" not in js
    assert "{ reason: alertReason(" in js
    # no delivery yet: show nothing at all
    assert 'if (!data.last_result || !data.last_result_ts) { return ""; }' in js


def test_secret_placeholders_are_masked():
    js = read("static", "app.js")
    assert 't("alerts.saved_tail", { tail: data.ntfy.topic_tail })' in js
    assert 't("alerts.saved_tail", { tail: data.webhook.path_tail })' in js
    assert "data.webhook.host +" not in js  # never print host:port/…tail
    assert "alerts.saved_plain" in js
    assert "alerts.saved_plain" in i18n.STRINGS["en"]
    assert "alerts.configured" not in js
    assert "alerts.last_result" not in i18n.STRINGS["en"]
    assert "alerts.never" not in i18n.STRINGS["en"]


def test_templates_have_no_inline_styles():
    for path in sorted(package_file("templates").glob("*.html")):
        text = path.read_text(encoding="utf-8")
        assert 'style="' not in text, path.name
