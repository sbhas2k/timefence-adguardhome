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


def test_the_group_test_table_has_no_hard_coded_english():
    app_js = read("static", "app.js")
    assert '? "blocked" : "allowed"' not in app_js
    assert "<th>host</th>" not in app_js
    for key in (
        "groups.test_host",
        "groups.test_adguard",
        "groups.test_blocked",
        "groups.test_allowed",
    ):
        assert f'"{key}"' in app_js, key
        assert i18n.t(key) != key


def test_service_icon_values_are_escaped_before_the_data_url():
    assert "esc(service.icon_svg)" in read("static", "app.js")


def test_the_banner_explains_rejected_credentials():
    app_js = read("static", "app.js")
    assert '"error.adguard_auth"' in app_js
    assert 'state.adguard_error_kind === "auth"' in app_js
    assert i18n.t("error.adguard_auth", seconds=30) != "error.adguard_auth"


def test_telegram_fields_are_masked_and_listed_first():
    html = read("templates", "app.html")
    js = read("static", "app.js")

    # the dropdown offers Telegram first, then ntfy, then the webhook
    assert html.index('value="telegram"') < html.index('value="ntfy"')
    assert html.index('value="ntfy"') < html.index('value="webhook"')

    token = next(
        line for line in html.splitlines() if "alerts-telegram-token" in line and "<input" in line
    )
    assert 'type="password"' in token
    assert 'autocomplete="new-password"' in token
    chat = next(
        line for line in html.splitlines() if "alerts-telegram-chat" in line and "<input" in line
    )
    assert 'type="text"' in chat
    assert 'inputmode="numeric"' in chat
    assert 'data-secret="telegram_token"' in html
    assert 'id="alerts-telegram"' in html
    assert 't("alerts.telegram_help")' in html

    # never the stored value, only a two-character tail
    assert 't("alerts.saved_tail", { tail: data.telegram.token_tail })' in js
    assert 't("alerts.saved_tail", { tail: data.telegram.chat_tail })' in js
    assert '["telegram_token", "#alerts-telegram-token"]' in js
    assert '["telegram_chat", "#alerts-telegram-chat"]' in js
    # the payload carries tails only, never a stored value
    assert not re.search(r"data\.telegram\.token(?!\w)", js)
    assert not re.search(r"data\.telegram\.chat(?!\w)", js)

    for key in (
        "alerts.channel_telegram",
        "alerts.telegram_token",
        "alerts.telegram_chat",
        "alerts.telegram_help",
        "alerts.clear_telegram_token",
    ):
        assert i18n.t(key) != key, key


def test_telegram_failures_get_their_own_sentences():
    js = read("static", "app.js")
    assert 'via === "telegram"' in js
    assert 'if (text === "http_401") { return t("alerts.reason_telegram_token"); }' in js
    for status in ('"http_400"', '"http_403"', '"http_404"'):
        assert status in js, status
    assert 't("alerts.reason_telegram_delivery")' in js
    assert "alertReason(data.last_result, data.channel)" in js
    for key in ("alerts.reason_telegram_token", "alerts.reason_telegram_delivery"):
        assert i18n.t(key) != key, key


def test_the_today_tab_offers_one_alerts_setup_hint():
    html = read("templates", "app.html")
    js = read("static", "app.js")

    assert 'id="alerts-hint"' in html
    assert 'id="alerts-setup"' in html
    assert 'id="alerts-card"' in html
    assert 't("alerts.hint")' in html
    assert 't("alerts.setup")' in html

    # fetched once, for admins only; a kid never asks for the config
    assert 'if (role !== "admin" || alertsHintLoaded) { return; }' in js
    assert "maybeLoadAlertsHint();" in js
    # shown while there is no channel or alerts are switched off
    assert "!config.channel || config.enabled === false" in js
    assert "paintAlertsHint();" in js
    # the button opens Settings and takes you to the card
    assert 'tab = "settings";' in js
    assert "scrollIntoView" in js

    for key in ("alerts.hint", "alerts.setup"):
        assert i18n.t(key) != key, key


def js_function(text: str, name: str) -> str:
    """The source of one top-level `function name(...)` in app.js."""
    start = text.index(f"  function {name}(")
    stop = text.find("\n  function ", start + 1)
    return text[start : stop if stop != -1 else len(text)]


def test_the_today_card_has_stable_header_live_and_controls_regions():
    """The header is patched in place, so a control placed there survives a poll."""
    js = read("static", "app.js")
    render = js_function(js, "renderToday")
    assert "card.appendChild(buildHeader());" in render
    assert "paintHeader(card, group);" in render
    assert "live.dataset.html !== liveMarkup" in render
    assert "header.innerHTML" not in js

    build = js_function(js, "buildHeader")
    for marker in (
        'dataset.headerEmoji = "1"',
        'dataset.headerName = "1"',
        'dataset.headerStatus = "1"',
        'dataset.headerCoverage = "1"',
    ):
        assert marker in build, marker

    paint = js_function(js, "paintHeader")
    for token in ("textContent = group.name", 'className = "pill " + status.cls', "dataset.status"):
        assert token in paint, token
    assert "[data-header-status]" in paint

    # the coverage line moved into the header; the live part keeps the numbers
    live = js_function(js, "liveHtml")
    assert "coverage(" not in live
    assert "group.name" not in live


def test_the_bonus_buttons_keep_focus_apart():
    """-15 and +15 are distinct after a re-render, so focus cannot move between them."""
    js = read("static", "app.js")
    names = js[js.index("var names = [") : js.index("];", js.index("var names = ["))]
    assert '"data-minutes"' in names
    assert 'data-minutes="-15"' in js
    assert 'data-minutes="15"' in js


def test_the_custom_bonus_box_is_cleared_only_after_a_successful_apply():
    js = read("static", "app.js")
    start = js.index('} else if (action === "bonus-custom") {')
    stop = js.index('} else if (action === "reset") {')
    custom = js[start:stop]
    assert "adminAction(" in custom
    assert custom.count('input.value = "";') == 1
    assert custom.index("adminAction(") < custom.index('input.value = "";')
    # the clear runs in the success callback, never on the invalid or failed path
    assert "function () {" in custom[custom.index("adminAction(") :]
    admin = js_function(js, "adminAction")
    assert "if (done) { done(data); }" in admin
    assert admin.index("done(data)") < admin.index(".catch(")


def test_the_free_time_default_is_an_hour_ahead_in_the_server_zone():
    js = read("static", "app.js")
    assert 'value="21:00"' not in js
    assert '"21:00"' not in js_function(js, "controlsHtml")
    assert "freeTimeValue(group)" in js_function(js, "controlsHtml")
    assert "defaultFreeTime()" in js_function(js, "freeTimeValue")

    minutes = js_function(js, "serverNowMinutes")
    assert "state.app.now" in minutes
    assert "state.app.timezone" in minutes

    default = js_function(js, "defaultFreeTime")
    assert "Math.ceil(" in default
    assert "/ 15" in default

    # a time that is not later than now is rolled to tomorrow by the server
    assert "freeTimeIsTomorrow" in js_function(js, "controlsHtml")
    assert 'data-free-tomorrow="' in js_function(js, "controlsHtml")
    assert "freeTimeIsTomorrow(input.value)" in js
    assert '"admin.free_tomorrow"' in js
    assert i18n.t("admin.free_tomorrow") != "admin.free_tomorrow"


def test_an_untouched_free_time_follows_the_server_clock():
    """A card drawn hours ago must not send a stale default when Allow is tapped."""
    js = read("static", "app.js")
    refresh = js_function(js, "refreshFreeDefault")
    # an edited value wins, and a focused input is never written to
    assert "if (freeTimeChoice[id]) { return; }" in refresh
    assert "document.activeElement === input" in refresh
    assert "input.value = value" in refresh
    assert "freeTimeIsTomorrow(value)" in refresh

    # it runs from the render path, after the controls are settled
    render = js_function(js, "renderToday")
    assert "refreshFreeDefault(card, group);" in render
    assert render.index("refreshFreeDefault(card, group);") > render.index("controls.innerHTML")

    # the click recomputes when nothing was edited, and uses the edit when there is one
    start = js.index('} else if (action === "free") {')
    stop = js.index('} else if (action === "clearfree") {')
    branch = js[start:stop]
    assert "freeTimeChoice[String(groupId)] || defaultFreeTime()" in branch
    assert "freeInput.value" in branch  # only as the last resort
    assert branch.index("defaultFreeTime()") < branch.index("freeInput.value")


def test_the_admin_tabs_are_one_scrolling_row_on_a_phone():
    """Four tabs, one row: a long translation scrolls sideways instead of stacking."""
    css = css_text()
    assert "grid-template-columns: 1fr 1fr" not in css
    tabs = rule(css, ".tabs")
    assert "display: flex" in tabs
    assert "flex-wrap: nowrap" in tabs
    assert "overflow-x: auto" in tabs

    # the mobile block keeps the taller tap target and never turns the row into a grid
    mobile = css[css.index("@media (max-width: 480px)") :]
    assert ".tabs" not in mobile[: mobile.index("@media (max-width: 360px)")]
    assert re.search(r"\.tab \{ font-size: 13px; padding: 10px 8px; \}", css)

    # a tab reached programmatically is scrolled into the strip, not the page
    assert 'inline: "nearest"' in read("static", "app.js")


def test_the_alerts_hint_is_one_slim_dismissible_banner():
    html = read("templates", "app.html")
    assert 'id="alerts-hint"' in html
    assert 'id="alerts-setup"' in html
    assert 't("alerts.hint")' in html
    assert 't("alerts.setup")' in html

    # a slim banner, not a full card
    assert re.search(r'<div class="[^"]*\bhint\b[^"]*" id="alerts-hint"', html)
    assert 'data-action="hint-dismiss"' in html
    for key in ("alerts.hint_dismiss", "alerts.hint_dismiss_label"):
        assert re.search(r"""t\(["']""" + re.escape(key) + r"""["']\)""", html), key
    assert i18n.t("alerts.hint_dismiss") == "✕"  # an icon; the label names it
    assert i18n.t("alerts.hint_dismiss_label") != i18n.t("alerts.hint_dismiss")

    css = css_text()
    hint = rule(css, ".hint")
    assert "display: flex" in hint
    assert "align-items: center" in hint
    assert "flex-wrap: nowrap" in hint  # one row: the buttons never drop below the text
    assert "min-height: 44px" in rule(css, ".hint .btn")


def test_the_alerts_hint_can_be_hidden_for_fourteen_days():
    js = read("static", "app.js")
    assert 'var HINT_HIDDEN_KEY = "tf.alertsHintHiddenAt";' in js
    assert "var HINT_HIDDEN_MS = 14 * 24 * 60 * 60 * 1000;" in js

    hidden_at = js_function(js, "hintHiddenAt")
    assert "localStorage.getItem(HINT_HIDDEN_KEY)" in hidden_at
    assert "isFinite(" in hidden_at

    dismissed = js_function(js, "hintDismissed")
    assert "hintHiddenAt()" in dismissed
    assert "Date.now()" in dismissed
    assert "HINT_HIDDEN_MS" in dismissed

    remembered = js_function(js, "rememberHintDismissal")
    assert "localStorage.setItem(HINT_HIDDEN_KEY" in remembered
    assert "Date.now()" in remembered

    paint = js_function(js, "paintAlertsHint")
    assert 'role === "admin"' in paint
    assert "!config.channel || config.enabled === false" in paint
    assert "!hintDismissed()" in paint

    start = js.index('} else if (action === "hint-dismiss") {')
    branch = js[start : js.index("} else if", start + 5)]
    assert "rememberHintDismissal();" in branch
    assert "paintAlertsHint();" in branch


def test_no_localstorage_call_escapes_its_try_block():
    js = read("static", "app.js")
    calls = list(re.finditer(r"localStorage\.(?:get|set|remove)Item", js))
    assert len(calls) >= 2
    for call in calls:
        guard = js.rindex("try {", 0, call.start())
        assert "}" not in js[guard : call.start()], "the call sits inside its try block"
        assert "} catch" in js[call.start() : call.start() + 400], "and the block has a catch"


def test_the_bonus_row_is_three_controls_with_a_custom_disclosure():
    """[-15 min] [+15 min] [Custom] on one row; the number box and Apply are disclosed."""
    js = read("static", "app.js")
    controls = js_function(js, "controlsHtml")

    row_start = controls.index("bonus-row")
    panel_start = controls.index("data-custom-panel")
    row = controls[row_start:panel_start]
    assert 'data-minutes="-15"' in row
    assert 'data-minutes="15"' in row
    assert 'data-action="custom"' in row  # the disclosure itself
    assert 'aria-expanded="false"' in row  # closed by default, state exposed to AT
    assert "aria-controls=" in row
    # the number box and Apply are disclosed, not in the always-visible row
    assert "data-custom-bonus" not in row
    assert 'data-action="bonus-custom"' not in row

    panel = controls[panel_start:]
    assert 'data-custom-bonus="' in panel
    assert 'data-action="bonus-custom"' in panel
    assert 'inputmode="numeric"' in panel
    assert 'step="5"' in panel
    assert 't("admin.custom_placeholder")' in panel
    # it starts collapsed (hidden class, never omitted from the markup)
    tag_start = controls.rindex("<div", 0, panel_start)
    assert "hidden" in controls[tag_start : controls.index(">", panel_start)]

    # the unblock control is Phase 3/4 work and stays exactly where it was, just outside the row
    assert 'data-action="unblock"' in controls


def test_the_custom_disclosure_stays_open_across_a_poll():
    """The open state belongs to the group, so a rebuilt controls region reopens it."""
    js = read("static", "app.js")
    assert "var openCustom = {};" in js

    paint = js_function(js, "paintCustom")
    assert "openCustom[id]" in paint
    assert 'classList.toggle("hidden", !open)' in paint
    assert 'setAttribute("aria-expanded"' in paint

    render = js_function(js, "renderToday")
    assert "paintCustom(card, group.id);" in render
    # re-applied after the controls markup was rebuilt
    assert render.index("paintCustom(card, group.id);") > render.index("controls.innerHTML")

    start = js.index('} else if (action === "custom") {')
    branch = js[start : js.index("} else if", start + 5)]
    assert "openCustom[String(groupId)] = !openCustom[String(groupId)];" in branch
    assert "paintCustom(" in branch

    # a successful Apply closes it again, right next to the cleared box
    start = js.index('} else if (action === "bonus-custom") {')
    custom = js[start : js.index('} else if (action === "reset") {')]
    assert "openCustom[String(groupId)] = false;" in custom
    assert custom.index("adminAction(") < custom.index("openCustom[String(groupId)] = false;")


def test_a_half_typed_custom_number_still_holds_the_controls():
    """The typing guard reads the box's value, whether the disclosure is open or closed."""
    js = read("static", "app.js")
    render = js_function(js, "renderToday")
    assert 'var input = controls.querySelector("[data-custom-bonus]");' in render
    assert "controls.contains(document.activeElement)" in render
    assert '(input && input.value !== "")' in render
    assert "if (card.dataset.signature !== signature && !typing) {" in render

    # painted, never removed: a closed box keeps whatever was typed in it
    paint = js_function(js, "paintCustom")
    assert "removeChild" not in paint
    assert "innerHTML" not in paint


def test_the_bonus_row_shares_the_width_on_a_phone():
    css = css_text()
    base = rule(css, ".bonus-row > *")
    assert "flex:" in base
    assert "min-width: 0" in base

    mobile = css[css.index("@media (max-width: 480px)") :]
    shared = rule(mobile, ".row.bonus-row > *")
    assert "flex: 1 1 0" in shared
    # every other wrapping row keeps its two-per-line rule
    assert "flex: 1 1 40%" in rule(mobile, ".row > .btn")
    assert "min-height: 44px" in rule(css, ".btn")


def test_every_bonus_control_is_named_for_its_group():
    """Five cards must not expose five identical "+15 min" buttons."""
    js = read("static", "app.js")
    controls = js_function(js, "controlsHtml")
    for key in (
        "admin.decrease_for",
        "admin.bonus_for",
        "admin.custom",
        "admin.custom_for",
        "admin.apply_for",
    ):
        assert f'"{key}"' in controls, key
        assert i18n.t(key) != key, key
    for key in ("admin.decrease_for", "admin.bonus_for", "admin.custom_for", "admin.apply_for"):
        assert "{name}" in i18n.STRINGS["en"][key], key
        assert f'esc(t("{key}", {{ name: group.name }}))' in controls, key
    # the visible words are unchanged
    assert 'esc(t("admin.decrease"))' in controls
    assert 'esc(t("admin.bonus"))' in controls
    assert 'esc(t("admin.apply"))' in controls
    assert i18n.t("admin.custom") == "Custom"


def test_a_busy_card_refuses_a_second_tap_without_losing_focus():
    """Disabling the pressed button drops focus to <body>; aria-disabled does not."""
    js = read("static", "app.js")
    busy = js_function(js, "setBusy")
    assert 'card.setAttribute("aria-busy", isBusy ? "true" : "false");' in busy
    assert '"aria-disabled"' in busy
    assert "button.disabled" not in busy

    # the inflight key is the double-submit protection now that nothing is disabled
    admin = js_function(js, "adminAction")
    assert "if (inflight[key]) { return; }" in admin
    assert "setBusy(groupId, 1);" in admin
    assert "setBusy(groupId, -1);" in admin

    start = js.index('document.addEventListener("click", function (event) {')
    handler = js[start : js.index('document.addEventListener("keydown"', start)]
    assert ('if (target.getAttribute("aria-disabled") === "true") { return; }') in handler

    css = css_text()
    style = rule(css, '.btn[aria-disabled="true"]')
    assert "opacity" in style
    assert "cursor: not-allowed" in style
