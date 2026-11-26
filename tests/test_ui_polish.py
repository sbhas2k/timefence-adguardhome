"""Static checks for the mobile and accessibility polish.

These read the shipped CSS, templates and scripts. They cannot measure a
rendered tap target or a wrapped line; the browser pre-check is the
authoritative evidence. They do fail if the relevant rule, branch or string is
removed, so a regression is caught in CI as well as by eye.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

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
    # show all, alerts enabled, and the three alert events left after slice U
    assert html.count('class="check"') >= 5
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
    # the pill is the one-word state; the live line carries the whole sentence
    assert "pill.textContent = status.short;" in paint
    status = js_function(js, "statusFor")
    for key in (
        "status.short_open",
        "status.short_closed",
        "status.short_free",
        "status.short_blocked",
        "status.short_paused",
    ):
        assert f't("{key}")' in status, key
        assert i18n.t(key) != key, key
    assert "status.text" not in paint

    # the coverage line moved into the header; the live part carries the schedule
    live = js_function(js, "liveHtml")
    assert "coverage(" not in live
    assert "group.name" not in live


def test_the_focus_key_names_the_live_schedule_controls_only():
    """The free panel and the segments keep focus; removed boxes are not keyed."""
    js = read("static", "app.js")
    names = js[js.index("var names = [") : js.index("];", js.index("var names = ["))]
    assert '"data-free-time"' in names
    assert '"data-mode"' in names
    assert '"data-action"' in names
    for gone in ('"data-minutes"', '"data-custom-bonus"'):
        assert gone not in names, gone
    for gone in ("data-minutes=", "data-custom-bonus="):
        assert gone not in js, gone


def test_the_today_controls_carry_no_minutes_bonus_or_reset():
    """The card is two controls: the mode segments and the Free panel."""
    js = read("static", "app.js")
    controls = js_function(js, "controlsHtml")
    for token in (
        "bonus-row",
        "data-custom-panel",
        'data-action="bonus"',
        'data-action="custom"',
        "data-minutes",
        'data-action="reset"',
        "minutes_today",
        "remaining_minutes",
        "bonus_minutes",
    ):
        assert token not in controls, token
    assert 'modeButton(group, "block"' in controls
    assert 'data-free-time="' in controls
    # no branch and no helper of the removed feature survives
    for branch in (
        'action === "bonus"',
        'action === "bonus-custom"',
        'action === "custom"',
        'action === "reset"',
    ):
        assert branch not in js, branch
    for name in (
        "bonusMessage",
        "paintCustom",
        "resetGroup",
        "fmtMinutes",
        "renderHistory",
        "loadUsage",
        "usageGroups",
    ):
        assert f"function {name}(" not in js, name
    assert "var openCustom = {};" not in js


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
    start = js.index('} else if (action === "free-apply") {')
    stop = js.index('} else if (action === "free-cancel") {')
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


def test_a_half_typed_free_time_still_holds_the_controls():
    """The typing guard protects the Free panel and nothing of the removed bonus UI."""
    js = read("static", "app.js")
    render = js_function(js, "renderToday")
    assert "controls.contains(document.activeElement)" in render
    assert "freeTimeChoice[id]" in render
    assert '(modeFor(group) === "free" && Boolean(freeTimeChoice[id]))' in render
    assert "if (card.dataset.signature !== signature && (!built || !typing)) {" in render
    assert "data-custom-bonus" not in render

    # painted, never removed: an open Free panel keeps whatever was typed in it
    paint = js_function(js, "paintMode")
    assert "removeChild" not in paint
    assert "innerHTML" not in paint


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


def test_the_windows_editor_fits_a_narrow_phone():
    """Start, end, days and Remove wrap instead of scrolling the page sideways."""
    css = css_text()
    row = rule(css, ".window-row")
    assert "display: flex" in row
    assert "flex-wrap: wrap" in row
    assert "gap" in row
    assert "min-width: 0" in row
    assert "max-width: 360px" in css
    assert "min-width: 702px" not in css  # never a fixed two-row grid wider than the phone
    assert "overflow-x: hidden" not in css  # content wraps; the page is never clipped


# ------------------------------------------------- the card's mode control


def test_the_mode_control_is_three_named_pressed_buttons():
    """Block | Auto | Free: buttons with aria-pressed in one role=group."""
    js = read("static", "app.js")
    controls = js_function(js, "controlsHtml")

    assert 'role="group"' in controls
    assert 'data-mode-group="' in controls
    assert 't("admin.mode_label", { name: group.name })' in controls
    # three plain buttons, never a native radio group that an arrow key drives
    assert 'type="radio"' not in js

    button = js_function(js, "modeButton")
    assert 'type="button"' in button
    assert 'aria-pressed="' in button
    for key in ("admin.mode_block", "admin.mode_auto", "admin.mode_free"):
        assert f't("{key}")' in button, key
        assert i18n.t(key) != key, key
    assert 'data-action="mode"' in button
    assert 'data-mode="' in button
    assert 'aria-pressed="' in button

    # the segments read Block, Auto, Free
    order = [controls.index(f'modeButton(group, "{m}"') for m in ("block", "auto", "free")]
    assert order == sorted(order)

    for key in ("admin.mode_label", "admin.mode_block", "admin.mode_auto", "admin.mode_free"):
        assert i18n.t(key) != key, key
    assert i18n.t("admin.mode_auto") == "Auto"
    assert "{name}" in i18n.STRINGS["en"]["admin.mode_label"]


def test_the_old_instant_mode_buttons_are_gone_from_the_card():
    """One control replaces Block now / Allow freely / Allow again / End free."""
    js = read("static", "app.js")
    controls = js_function(js, "controlsHtml")
    for key in ("admin.block_now", "admin.free_apply", "admin.unblock", "admin.clear_free"):
        assert key not in controls, key
    for action in ("block", "free", "clearfree", "unblock"):
        assert f'data-action="{action}"' not in controls, action

    # the handler no longer has a branch of its own for them
    for branch in ('action === "clearfree"', 'action === "unblock"'):
        assert branch not in js, branch


def test_the_free_segment_only_opens_a_panel():
    """Free is two-step: the tap stores a pending choice and sends nothing."""
    js = read("static", "app.js")
    assert "var pendingFree = {};" in js

    start = js.index('if (action === "mode") {')
    branch = js[start : js.index("} else if", start + 5)]
    open_leg = branch[branch.index('if (to === "free") {') :]
    open_leg = open_leg[: open_leg.index("return;")]
    assert "pendingFree[String(groupId)]" in open_leg
    assert "paintMode(" in open_leg
    assert "api(" not in open_leg
    assert "setMode(" not in open_leg

    # the panel holds the time box, its hint, Extra time, Clear and a Cancel
    controls = js_function(js, "controlsHtml")
    assert 'data-free-panel="' in controls
    assert 'data-free-time="' in controls
    assert 'data-free-tomorrow="' in controls
    assert 'data-action="free-apply"' in controls
    assert 'esc(t("admin.extra_time"))' in controls
    assert 'data-action="free-clear"' in controls
    assert 'data-action="free-cancel"' in controls
    assert 'esc(t("common.cancel"))' in controls
    assert i18n.t("admin.extra_time") != "admin.extra_time"

    apply_start = js.index('if (action === "free-apply") {')
    apply = js[apply_start : js.index("} else if", apply_start + 5)]
    assert 'setMode(groupId, "free", until);' in apply
    cancel_start = js.index('if (action === "free-cancel") {')
    cancel = js[cancel_start : js.index("} else if", cancel_start + 5)]
    assert "pendingFree[String(groupId)] = false;" in cancel
    assert "api(" not in cancel


def test_the_free_panel_shows_the_current_end_and_the_stored_block_note():
    js = read("static", "app.js")
    controls = js_function(js, "controlsHtml")
    assert 'data-free-note="' in controls
    assert 'esc(t("admin.free_then_note"))' in controls
    assert i18n.t("admin.free_then_note") != "admin.free_then_note"
    # no Cancel while the group is already free: only the time can change
    assert 'free ? " hidden" : ""' in controls

    # a free group's box shows its current end, in the server zone, as HH:MM
    value = js_function(js, "freeTimeValue")
    assert 'group.status === "free"' in value
    assert "group.free_until" in value
    assert "defaultFreeTime()" in value  # the pinned fallback survives
    assert "hhmm(" in value
    hhmm = js_function(js, "hhmm")
    assert 'hourCycle: "h23"' in hhmm
    assert 'minute: "2-digit"' in hhmm


def test_set_mode_runs_the_calls_in_order_with_one_refresh_and_one_toast():
    """Two-call moves are sequential under one key, one refresh, one toast."""
    js = read("static", "app.js")
    set_mode = js_function(js, "setMode")
    assert 'var key = "mode:" + groupId;' in set_mode
    assert "if (inflight[key]) { return; }" in set_mode
    assert "modeSequence(from, to, group)" in set_mode
    assert set_mode.count("refresh()") == 1
    assert "toast(modeMessage(from, to, group.name, until));" in set_mode
    assert "steps.forEach(" in set_mode
    assert "if (failed) { return null; }" in set_mode
    assert "delete inflight[key];" in set_mode
    assert "setBusy(groupId, 1);" in set_mode
    assert "setBusy(groupId, -1);" in set_mode
    # the route is built from the suffix table, never hard-coded per transition
    assert '"/api/groups/" + groupId + "/" + step' in set_mode
    assert "force/clear" not in set_mode
    assert "free/clear" not in set_mode

    message = js_function(js, "modeMessage")
    for key in ("toast.blocked", "toast.free", "toast.unblocked", "toast.free_cleared"):
        assert f't("{key}"' in message, key
        assert i18n.t(key) != key, key


def mode_probe_script(js: str) -> str:
    """The two pure helpers plus a table, so node can prove the behaviour."""
    cases = [
        ("auto", "block", 0),
        ("block", "auto", 0),
        ("auto", "free", 0),
        ("block", "free", 0),
        ("free", "block", 0),
        ("free", "auto", 0),
        ("free", "auto", 1999999999),
        ("free", "free", 0),
        ("block", "block", 0),
        ("auto", "auto", 0),
    ]
    rows = ",".join(f"[{json.dumps(a)},{json.dumps(b)},{c}]" for a, b, c in cases)
    return "\n".join(
        [
            js_function(js, "modeFor"),
            js_function(js, "modeSequence"),
            f"var cases = [{rows}];",
            "var seq = cases.map(function (c) {",
            "  return modeSequence(c[0], c[1], {",
            '    paused: false, status: "allowed", force_block_until: c[2] });',
            "});",
            "var statuses = [",
            '  { paused: true, status: "paused" },',
            '  { paused: false, status: "free" },',
            '  { paused: false, status: "blocked_admin" },',
            '  { paused: false, status: "allowed" },',
            '  { paused: false, status: "outside_hours" }',
            "];",
            "var modes = statuses.map(modeFor);",
            "console.log(JSON.stringify({ seq: seq, modes: modes }));",
        ]
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_mode_derivation_and_every_transition_in_order():
    """The whole transition table, executed: the mode shown and the call order."""
    js = read("static", "app.js")
    done = subprocess.run(
        [shutil.which("node"), "-e", mode_probe_script(js)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)

    assert got["seq"] == [
        ["block"],  # Auto -> Block
        ["force/clear"],  # Block -> Auto
        ["free"],  # Auto -> Free
        ["free", "force/clear"],  # Block -> Free, then back to Auto after it
        ["block"],  # Free -> Block, the server clears the free window
        ["free/clear"],  # Free -> Auto, nothing stored underneath
        ["force/clear", "free/clear"],  # Free + stored block -> Auto, this order
        ["free"],  # Free -> Free, a new time
        [],  # the pressed Block does nothing
        [],  # the pressed Auto does nothing
    ]
    # paused wins; a free window beats a block; every other status is Auto
    assert got["modes"] == ["", "free", "block", "auto", "auto"]


def controls_probe_script(js: str) -> str:
    """controlsHtml with stubs, so node can check the markup it really builds."""
    return "".join(
        [
            "var pendingFree = {};",
            "function t(key) { return key; }",
            'function esc(value) { return String(value == null ? "" : value); }',
            'function freeTimeValue() { return "21:00"; }',
            "function freeTimeIsTomorrow() { return false; }",
            js_function(js, "modeFor"),
            js_function(js, "modeButton"),
            js_function(js, "controlsHtml"),
            "var cases = [",
            '  { id: 1, name: "YouTube", paused: false, status: "allowed", force_block_until: 0 },',
            '  { id: 2, name: "Games", paused: false, status: "free",',
            "    force_block_until: 1999999999 },",
            '  { id: 3, name: "Tablet", paused: true, status: "paused", force_block_until: 0 }',
            "];",
            "var out = cases.map(function (group) {",
            "  var html = controlsHtml(group);",
            "  var open = (html.match(/<div/g) || []).length;",
            r"  var close = (html.match(/<\/div>/g) || []).length;",
            "  return { open: open, close: close, html: html };",
            "});",
            "console.log(JSON.stringify(out));",
        ]
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_controls_markup_is_balanced_and_ordered():
    """Rendered with stubs: every region is closed, and the regions keep their order."""
    js = read("static", "app.js")
    done = subprocess.run(
        [shutil.which("node"), "-e", controls_probe_script(js)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    cards = json.loads(done.stdout)
    for card in cards:
        assert card["open"] == card["close"], card["html"]

    allowed, free, paused = (card["html"] for card in cards)
    order = [
        allowed.index('data-action="mode"'),
        allowed.index("data-free-panel="),
        allowed.index("free-note"),
    ]
    assert order == sorted(order)
    # no bonus row and no reset trigger is left in the controls, in any state
    for card in (allowed, free, paused):
        for token in ('data-action="reset"', "bonus-row", "data-custom-bonus", "data-minutes"):
            assert token not in card, (token, card)
    # a free card shows its panel open, with no Cancel, and the stored-block note
    assert 'data-free-panel="2"' in free
    assert 'class="row tight mt free-panel' in free
    assert "free-panel hidden" not in free
    assert 'class="btn hidden" data-action="free-cancel"' in free
    assert 'free-note" data-free-note="2"' in free
    assert "free-note hidden" not in free
    # a paused card keeps one button and nothing else
    assert 'data-action="resume"' in paused
    assert 'data-action="mode"' not in paused
    for token in ("bonus-row", 'data-action="reset"', "data-free-time"):
        assert token not in paused, token


def test_midnight_makes_the_stored_block_invisible():
    """The payload reports force_block_until 0 once it expired, so Auto is shown."""
    js = read("static", "app.js")
    seq = js_function(js, "modeSequence")
    assert "Number(group.force_block_until) > 0" in seq
    # the note is driven by the same test, never by a clock in the browser
    paint = js_function(js, "paintMode")
    assert "Number(group.force_block_until) > 0" in paint


def test_the_pressed_segment_follows_the_server_on_every_poll():
    """Patched in place outside the typing guard, so an expiry flips at once."""
    js = read("static", "app.js")
    # the three segments share data-action, so the focus key needs the mode
    names = js[js.index("var names = [") : js.index("];", js.index("var names = ["))]
    assert '"data-mode"' in names
    paint = js_function(js, "paintMode")
    assert "modeFor(group)" in paint
    assert 'button.dataset.mode === mode ? "true" : "false"' in paint
    assert 'setAttribute("aria-pressed"' in paint
    assert "innerHTML" not in paint
    assert "removeChild" not in paint
    assert 'data-free-panel="' in paint
    assert 'classList.toggle("hidden", !open)' in paint

    render = js_function(js, "renderToday")
    assert "paintMode(card, group);" in render
    # outside the guard, and after the controls may have been rebuilt
    assert render.index("paintMode(card, group);") > render.index("controls.innerHTML")
    assert "\n      paintMode(card, group);\n" in render
    # a pending Free panel or an edited time counts as typing, so the time box is
    # never rebuilt under the admin; a group that is merely Free does not
    assert (
        "Boolean(pendingFree[id]) ||" in render
        and '(modeFor(group) === "free" && Boolean(freeTimeChoice[id]))' in render
    )
    assert "|| openFree;" in render

    signature = js_function(js, "signatureFor")
    assert "modeFor(group)" in signature
    assert "group.free_until" in signature
    assert "Number(group.force_block_until) > 0" in signature


def test_a_paused_group_shows_one_resume_button():
    js = read("static", "app.js")
    controls = js_function(js, "controlsHtml")
    paused = controls[
        controls.index("if (group.paused)") : controls.index("var mode = modeFor(group)")
    ]
    assert 'data-action="resume"' in paused
    assert 'esc(t("admin.resume"))' in paused
    assert 't("admin.resume_for", { name: group.name })' in paused
    for token in ('role="group"', 'data-action="bonus"', 'data-action="reset"', "data-free-time"):
        assert token not in paused, token
    assert i18n.t("admin.resume") == "Resume"
    assert "{name}" in i18n.STRINGS["en"]["admin.resume_for"]

    start = js.index('if (action === "resume") {')
    branch = js[start : js.index("} else if", start + 5)]
    assert 'adminAction("pause", groupId, { paused: false }' in branch
    assert 't("toast.resumed", { name: name })' in branch

    # the controls of a paused card carry no mode at all, so nothing is patched
    paint = js_function(js, "paintMode")
    assert 'data-action="mode"' in paint


def test_arrow_keys_move_focus_between_the_segments_only():
    js = read("static", "app.js")
    start = js.index("/* Arrow keys move focus")
    handler = js[start : start + 1100]
    for key in ('"ArrowRight"', '"ArrowLeft"', '"Home"', '"End"'):
        assert key in handler, key
    assert "event.preventDefault();" in handler
    assert "next.focus();" in handler
    assert "click()" not in handler  # arrows never apply a mode
    assert "setMode(" not in handler
    assert "data-mode-group" in handler


def test_the_mode_control_is_44_pixels_and_never_colour_only():
    css = css_text()
    seg = rule(css, ".seg")
    assert "display: flex" in seg
    button = rule(css, ".seg > .seg-btn")
    assert "flex: 1 1 0" in button
    assert "min-height: 44px" in button or "min-height: 46px" in button
    assert "min-width: 0" in button
    pressed = rule(css, '.seg-btn[aria-pressed="true"]')
    assert "border-color" in pressed
    assert "box-shadow" in pressed  # a ring, not colour alone
    assert "font-weight" in pressed
    assert "color-mix(" in pressed


def test_a_group_that_loads_already_free_still_gets_its_controls():
    """The typing guard protects built controls; it must never block the first build."""
    js = read("static", "app.js")
    start = js.index("var openFree =")
    block = js[start : js.index("paintMode(card, group);", start)]
    # the first build is never blocked by the guard
    assert "var built = " in block
    assert "!built ||" in block
    # a group that is merely Free is not "typing": only a pending panel or an edited time is
    assert '|| modeFor(group) === "free"' not in block
    assert "freeTimeChoice[id]" in block


# --------------------------------------- the card header: reset + coverage


def coverage_probe_script(js: str) -> str:
    """coverageCount and coverageSummary with the real English plural strings."""
    keys = ("card.apps_one", "card.apps_other", "card.domains_one", "card.domains_other")
    return "".join(
        [
            "var i18n = " + json.dumps({key: i18n.STRINGS["en"][key] for key in keys}) + ";",
            "function t(key, vars) {",
            r"  return String(i18n[key] || key).replace(/\{(\w+)\}/g, function (whole, name) {",
            "    return vars && Object.prototype.hasOwnProperty.call(vars, name)",
            "      ? String(vars[name]) : whole;",
            "  });",
            "}",
            js_function(js, "coverageCount"),
            js_function(js, "coverageSummary"),
            "var cases = [",
            '  { services: ["YouTube"], domains: [] },',
            '  { services: ["YouTube", "Netflix", "TikTok"], domains: ["a.com", "b.com"] },',
            '  { services: [], domains: ["x.com"] },',
            "  { services: [], domains: [] }",
            "];",
            "console.log(JSON.stringify(cases.map(function (group) {",
            "  return [coverageCount(group), coverageSummary(group)];",
            "})));",
        ]
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_coverage_summary_counts_apps_and_domains_in_words():
    """Executed: the one-line summary omits a zero part and gets singulars right."""
    js = read("static", "app.js")
    done = subprocess.run(
        [shutil.which("node"), "-e", coverage_probe_script(js)],
        capture_output=True,
        text=True,
        # the summary carries a middle dot, so the decode must not be the locale
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [
        [1, "1 app"],
        [5, "3 apps · 2 domains"],
        [1, "1 domain"],
        [0, ""],
    ]


def test_the_coverage_summary_strings_are_plural_pairs():
    for one, other in (
        ("card.apps_one", "card.apps_other"),
        ("card.domains_one", "card.domains_other"),
    ):
        assert one in i18n.STRINGS["en"], one
        assert other in i18n.STRINGS["en"], other
        singular = i18n.STRINGS["en"][one]
        plural = i18n.STRINGS["en"][other]
        assert singular.startswith("{count} ")
        assert plural == singular + "s"
    summary = js_function(read("static", "app.js"), "coverageSummary")
    for key in (
        "card.apps_one",
        "card.apps_other",
        "card.domains_one",
        "card.domains_other",
    ):
        assert f't("{key}"' in summary, key


def test_the_coverage_line_collapses_above_three_items():
    """More than three items gets a one-line summary; the open state is local."""
    js = read("static", "app.js")
    assert "var openCoverage = {};" in js

    count = js_function(js, "coverageCount")
    assert "services" in count
    assert "domains" in count

    # built once, patched afterwards: a poll can neither collapse nor replace it
    build = js_function(js, "buildHeader")
    assert 'toggle.dataset.action = "coverage";' in build
    assert 'toggle.setAttribute("aria-expanded", "false");' in build
    assert 'chev.setAttribute("aria-hidden", "true");' in build
    assert 'line.dataset.headerCoverage = "1"' in build

    paint = js_function(js, "paintHeader")
    assert "coverageCount(group) > 3" in paint
    assert "Boolean(openCoverage[id])" in paint
    assert 'toggle.setAttribute("aria-expanded", open ? "true" : "false");' in paint
    assert 'toggle.setAttribute("aria-controls", "coverage-" + id);' in paint
    assert 'list.id = "coverage-" + id;' in paint
    assert 'toggle.querySelector("[data-header-summary]").textContent =' in paint
    assert "coverageSummary(group)" in paint
    assert "list.textContent = coverage(group);" in paint
    assert 'toggle.classList.toggle("hidden", !collapsible);' in paint
    assert 'list.classList.toggle("hidden", collapsible && !open);' in paint
    assert "innerHTML" not in paint

    # the toggle is reachable per group, like the reset icon beside it
    assert "toggle.dataset.group = id;" in paint

    start = js.index('} else if (action === "coverage") {')
    branch = js[start : js.index("} else if", start + 5)]
    assert "openCoverage[String(groupId)] = !openCoverage[String(groupId)];" in branch
    assert "paintHeader(" in branch


def test_the_coverage_toggle_is_a_44_pixel_target():
    css = css_text()
    toggle = rule(css, ".coverage-toggle")
    assert "min-height: 44px" in toggle
    assert "inline-flex" in toggle
    assert "background: none" in toggle
    opened = rule(css, '.coverage-toggle[aria-expanded="true"] .chev')
    assert "rotate(" in opened


def test_the_card_header_carries_no_reset_trigger():
    """The reset route is gone, so its icon, dialog and strings are gone too."""
    js = read("static", "app.js")
    assert 'data-action="reset"' not in js
    assert "admin.reset_today" not in js
    assert "admin.reset_for" not in js
    assert "admin.reset_confirm" not in js
    assert "↺" not in js
    for key in ("admin.reset_today", "admin.reset_for", "admin.reset_confirm"):
        assert key not in i18n.STRINGS["en"], key
    build = js_function(js, "buildHeader")
    assert "card-reset" not in build
    assert "reset" not in build.lower()


def test_the_card_header_wraps_a_long_name():
    css = css_text()
    head = rule(css, ".card-head")
    assert "display: flex" in head
    assert "gap" in head
    title = rule(css, ".card-head h2")
    assert "min-width: 0" in title  # a long name wraps instead of pushing the card wider
    assert "overflow-wrap: anywhere" in title
    assert ".card-reset" not in css


def test_cancelling_the_free_panel_releases_the_controls():
    """A cancelled edit must not leave the card's controls frozen for good."""
    js = read("static", "app.js")
    cancel = js[js.index('action === "free-cancel"') : js.index('action === "resume"')]
    assert "delete freeTimeChoice[String(groupId)]" in cancel
    # tapping Block or Auto abandons a half-edited Free time as well
    mode = js[js.index('if (action === "mode")') : js.index('action === "free-apply"')]
    assert "delete freeTimeChoice[String(groupId)]" in mode
    # an edited time only protects the controls while the group is Free or the panel is pending
    render = js_function(js, "renderToday")
    assert '(modeFor(group) === "free" && Boolean(freeTimeChoice[id]))' in render


def test_renaming_a_group_rebuilds_its_card_controls():
    """The buttons carry the group name in their accessible names."""
    js = read("static", "app.js")
    assert "group.name" in js_function(js, "signatureFor")


def test_the_free_segment_names_its_panel_and_moves_focus_into_it():
    js = read("static", "app.js")
    button = js_function(js, "modeButton")
    assert 'aria-controls="free-panel-' in button
    controls = js_function(js, "controlsHtml")
    assert "id=\"free-panel-' + group.id" in controls
    mode = js[js.index('if (action === "mode")') : js.index('action === "free-apply"')]
    assert "[data-free-time=" in mode and ".focus()" in mode


def test_the_free_panel_offers_extra_time_and_a_clear_button():
    """Extra time is the one-tap override; Clear ends it on the existing route."""
    js = read("static", "app.js")
    controls = js_function(js, "controlsHtml")
    # the apply button of the Free panel is the Extra time action
    assert 'esc(t("admin.extra_time"))' in controls
    assert 'data-action="free-apply"' in controls
    # Clear exists only when the group is already free, and it is a separate action
    assert 'data-action="free-clear"' in controls
    assert 'free ? "" : " hidden"' in controls
    assert 'esc(t("admin.free_clear"))' in controls
    # a free group's panel is open, so Clear is reachable without opening a menu
    assert 'var free = mode === "free";' in controls
    assert "var open = free || Boolean(pendingFree[id]);" in controls

    start = js.index('if (action === "free-clear") {')
    branch = js[start : js.index("} else if", start + 5)]
    assert 'adminAction("free/clear"' in branch
    assert 't("toast.free_cleared"' in branch
    for key in ("admin.extra_time", "admin.free_clear"):
        assert i18n.t(key) != key, key


def test_the_today_header_offers_block_all_and_release_all_to_admins():
    """Two whole-house actions, in the Today header, behind a confirm dialog."""
    html = read("templates", "app.html")
    assert 'id="today-actions"' in html
    assert 'id="block-all"' in html
    assert 'id="release-all"' in html
    assert 't("header.block_all")' in html
    assert 't("header.release_all")' in html
    for key in (
        "header.block_all",
        "header.release_all",
        "toast.blocked_all",
        "toast.released_all",
    ):
        assert i18n.t(key) != key, key

    js = read("static", "app.js")
    # a user account never sees the buttons, and the server refuses the calls
    render = js_function(js, "render")
    assert 'todayActions.classList.toggle("hidden", !isAdmin);' in render
    action = js_function(js, "allGroupsAction")
    assert 't("common.confirm")' in action
    assert 't("common.yes")' in action
    assert '"/api/" + (kind === "block" ? "block-all" : "release-all")' in action
    assert "refresh()" in action
    for key in ("block-all", "release-all"):
        assert f'$("#{key}")' in js, key


# ------------------------------------------------ the allowed-times schedule
# The backend has no daily minutes any more: a group is open inside its
# allowed windows (0-4 of them, no windows = always open) and closed outside.


def user_group_keys() -> tuple[str, ...]:
    from timefence.api import USER_GROUP_KEYS

    return USER_GROUP_KEYS


def test_the_ui_reads_no_removed_field_and_calls_no_removed_route():
    """No leftover of the minutes budget can hit a 404 or show a blank."""
    banned = (
        "bonus",
        "usage",
        "history",
        "remaining_minutes",
        "weekday_minutes",
        "weekend_minutes",
        "bedtime",
        "limit_reached",
        "metered",
        "minutes_today",
        "used_minutes",
    )
    for name in ("app.js", "kid.js"):
        text = read("static", name)
        for token in banned:
            assert token not in text, (name, token)
    for token in ("/api/usage", "usage.csv", "/bonus", "/reset"):
        assert token not in read("static", "app.js"), token

    for path in sorted(package_file("templates").glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, (path.name, token)
        for token in ("/api/usage", "/bonus", "/reset"):
            assert token not in text, (path.name, token)

    # the dead i18n families went with the markup that used them
    removed = (
        "admin.bonus",
        "admin.bonus_for",
        "admin.decrease",
        "admin.decrease_for",
        "admin.custom",
        "admin.custom_for",
        "admin.custom_placeholder",
        "admin.custom_invalid",
        "admin.apply",
        "admin.apply_for",
        "admin.reset_today",
        "admin.reset_for",
        "admin.reset_confirm",
        "admin.tab.history",
        "admin.block_now",
        "admin.unblock",
        "admin.clear_free",
        "admin.free_apply",
        "budget.left",
        "budget.used",
        "status.allowed_auto",
        "status.limit_reached",
        "status.bedtime",
        "status.bedtime_any",
        "toast.bonus",
        "toast.bonus_removed",
        "toast.reset",
        "history.heading",
        "history.range",
        "history.days",
        "history.download",
        "history.none",
        "history.day",
        "history.total",
        "kid.minutes_left",
        "kid.less_than_minute",
        "kid.time_up",
        "kid.back_tomorrow",
        "kid.blocked_by_parent",
        "kid.bedtime",
        "kid.bedtime_any",
        "kid.used_today",
        "warn.querylog_disabled",
        "warn.querylog_retention_short",
        "warn.querylog_ignores_group",
        "warn.querylog_truncated",
        "warn.session_groups_converted",
        "alerts.event.group_low",
        "alerts.event.group_exhausted",
        "alerts.event.bedtime_started",
        "alerts.event.weekly_summary",
        "error.no_budget",
        "error.usage_days",
        "groups.weekday",
        "groups.weekend",
        "groups.weekend_same",
        "groups.bedtime_heading",
        "groups.bedtime_help",
        "groups.bedtime_add",
        "groups.bedtime_from",
        "groups.bedtime_until",
        "groups.bedtime_days",
        "groups.bedtime_remove",
        "groups.bedtime_days_all",
        "groups.bedtime_days_weekday",
        "groups.bedtime_days_weekend",
        "common.minutes_short",
        "common.back",
    )
    still_there = sorted(key for key in removed if key in i18n.STRINGS["en"])
    assert still_there == []
    words = " ".join(i18n.STRINGS["en"].values()).lower()
    for word in ("bonus", "usage", "history", "bedtime", "metered", "minutes left"):
        assert word not in words, word


def test_the_allowed_times_editor_replaces_the_minutes_fields():
    js = read("static", "app.js")
    editor = js[js.index("function openGroupEditorWith(") : js.index("function openServicePicker(")]
    assert 'id="allowed-windows"' in editor
    assert 'data-action="add-window"' in editor
    assert 'data-window-start="' in editor
    assert 'data-window-end="' in editor
    assert 'data-window-days="' in editor
    assert 'data-action="remove-window"' in editor
    assert 't("groups.allowed_heading")' in editor
    assert 't("groups.allowed_help")' in editor

    # four windows is the cap, enforced by the add handler
    assert "windows.length >= 4" in editor
    # the helper text the owner asked for, word for word
    assert (
        i18n.t("groups.allowed_help")
        == "Outside these times the group is blocked. No times = always allowed."
    )

    # no minutes field or bedtime concept survives anywhere in the editor
    for token in ("g-weekday", "g-weekend", "weekday_minutes", "weekend_minutes", "bedtime"):
        assert token not in editor, token
    # the form sends the windows and nothing else about time
    payload_start = editor.index("var payload = {")
    payload = editor[payload_start : editor.index("};", payload_start)]
    assert "windows: readWindows()" in payload
    for token in ("minutes", "weekday", "weekend"):
        assert token not in payload, token
    # the seven day chips plus Every day: the values the server validates
    assert 'var WEEK = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];' in editor
    assert 'chip("all"' in editor
    assert "aria-pressed" in editor
    assert "data-window-days=" in editor

    for key in (
        "groups.allowed_heading",
        "groups.allowed_help",
        "groups.allowed_add",
        "groups.window_from",
        "groups.window_until",
        "groups.window_days",
        "groups.window_remove",
        "groups.days_all",
        "groups.days_mon",
        "groups.days_tue",
        "groups.days_wed",
        "groups.days_thu",
        "groups.days_fri",
        "groups.days_sat",
        "groups.days_sun",
        "groups.day_mon",
        "groups.day_tue",
        "groups.day_wed",
        "groups.day_thu",
        "groups.day_fri",
        "groups.day_sat",
        "groups.day_sun",
    ):
        # the day keys live in the chip-label maps, so the literal name is enough
        assert f'"{key}"' in editor, key
        assert i18n.t(key) != key, key
    # a new group starts with no allowed times at all, which means always open
    assert "windows: []," in editor


def live_probe_script(js: str) -> str:
    """statusFor and liveHtml with the real strings, so node can prove the text."""
    keys = (
        "status.paused",
        "status.free",
        "status.forced",
        "status.open_until",
        "status.opens_at",
        "status.open_all_day",
    )
    cases = [
        {
            "id": 1,
            "status": "allowed",
            "open_until": "21:00",
            "opens_at": None,
            "windows": [{"start": "15:00", "end": "21:00", "days": "all"}],
        },
        {"id": 2, "status": "allowed", "open_until": None, "opens_at": None, "windows": []},
        {
            "id": 3,
            "status": "outside_hours",
            "open_until": None,
            "opens_at": "14:00",
            "windows": [{"start": "14:00", "end": "16:00", "days": "sat,sun"}],
        },
        {"id": 4, "status": "free", "free_until": 1999999999, "windows": []},
        {"id": 5, "status": "blocked_admin", "force_block_until": 1999999999, "windows": []},
        {"id": 6, "status": "paused", "paused": True, "windows": []},
    ]
    return "".join(
        [
            "var i18n = " + json.dumps({key: i18n.STRINGS["en"][key] for key in keys}) + ";",
            "function t(key, vars) {",
            r"  return String(i18n[key] || key).replace(/\{(\w+)\}/g, function (whole, name) {",
            "    return vars && Object.prototype.hasOwnProperty.call(vars, name)",
            "      ? String(vars[name]) : whole;",
            "  });",
            "}",
            'function esc(value) { return String(value == null ? "" : value); }',
            'function clock() { return "07:30"; }',
            js_function(js, "statusFor"),
            js_function(js, "liveHtml"),
            "var cases = " + json.dumps(cases) + ";",
            "console.log(JSON.stringify(cases.map(function (group) {",
            "  return [statusFor(group).text, liveHtml(group)];",
            "})));",
        ]
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_live_row_names_the_schedule_from_the_payload():
    """Executed: one sentence per status, built only from server fields."""
    js = read("static", "app.js")
    done = subprocess.run(
        [shutil.which("node"), "-e", live_probe_script(js)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [
        ["Open until 21:00", '<p class="live-line ok">Open until 21:00</p>'],
        ["Open all day", '<p class="live-line ok">Open all day</p>'],
        ["Opens at 14:00", '<p class="live-line warn">Opens at 14:00</p>'],
        ["Free until 07:30", '<p class="live-line warn">Free until 07:30</p>'],
        ["Blocked until midnight", '<p class="live-line bad">Blocked until midnight</p>'],
        ["Paused", '<p class="live-line warn">Paused</p>'],
    ]


def test_the_live_row_has_no_clock_maths_and_no_budget():
    js = read("static", "app.js")
    status = js_function(js, "statusFor")
    for token in ("open_until", "open_all_day", "opens_at", 'group.status === "outside_hours"'):
        assert token in status, token
    for token in ("Date.now()", "minutes", "progress", "remaining"):
        assert token not in status, token
    live = js_function(js, "liveHtml")
    assert "statusFor(group)" in live
    assert "<progress" not in live
    assert "coverage(" not in live
    assert "group.name" not in live


WINDOWS_KEYS = (
    "card.windows_line",
    "card.window_range",
    "card.window_days_all",
    "card.window_days_some",
    "card.day_mon",
    "card.day_tue",
    "card.day_wed",
    "card.day_thu",
    "card.day_fri",
    "card.day_sat",
    "card.day_sun",
)


def windows_probe_script(js: str) -> str:
    """liveHtml and windowsLineHtml with the real strings and the real esc."""
    keys = (
        "status.open_until",
        "status.open_all_day",
        "status.opens_at",
        "status.paused",
    ) + WINDOWS_KEYS
    cases = [
        {
            "id": 1,
            "status": "allowed",
            "open_until": "21:00",
            "windows": [
                {"start": "14:00", "end": "15:00", "days": "all"},
                {"start": "18:00", "end": "19:00", "days": "all"},
            ],
        },
        {
            "id": 2,
            "status": "outside_hours",
            "opens_at": "08:00",
            "windows": [
                {"start": "08:00", "end": "09:00", "days": "mon,tue,wed,thu,fri"},
                {"start": "10:00", "end": "11:00", "days": "sat,sun"},
            ],
        },
        {"id": 3, "status": "allowed", "open_until": None, "windows": []},
        {
            "id": 4,
            "status": "paused",
            "paused": True,
            "windows": [{"start": "16:00", "end": "17:00", "days": "all"}],
        },
        {
            "id": 5,
            "status": "allowed",
            "open_until": None,
            "windows": [{"start": "13:00\"><script>&'", "end": "14:00", "days": "all"}],
        },
    ]
    return "".join(
        [
            "var i18n = " + json.dumps({key: i18n.STRINGS["en"][key] for key in keys}) + ";",
            "function t(key, vars) {",
            r"  return String(i18n[key] || key).replace(/\{(\w+)\}/g, function (whole, name) {",
            "    return vars && Object.prototype.hasOwnProperty.call(vars, name)",
            "      ? String(vars[name]) : whole;",
            "  });",
            "}",
            js_function(js, "esc"),
            js_function(js, "statusFor"),
            js_function(js, "liveHtml"),
            js_function(js, "windowDaysLabel"),
            js_function(js, "windowsLineHtml"),
            "var cases = " + json.dumps(cases) + ";",
            "console.log(JSON.stringify(cases.map(function (group) {",
            "  return [liveHtml(group), windowsLineHtml(group)];",
            "})));",
        ]
    )


def run_windows_probe(js: str) -> list:
    done = subprocess.run(
        [shutil.which("node"), "-e", windows_probe_script(js)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_today_card_lists_the_allowed_windows():
    """The allowed windows already in the payload get one muted line."""
    rows = run_windows_probe(read("static", "app.js"))
    assert rows[0][0] == '<p class="live-line ok">Open until 21:00</p>'
    assert rows[0][1] == (
        '<p class="muted small live-windows">Allowed 14:00–15:00 · 18:00–19:00</p>'
    )
    # paused and blocked cards keep the schedule they would run next
    assert rows[3][0] == '<p class="live-line warn">Paused</p>'
    assert "16:00–17:00" in rows[3][1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_windows_line_names_the_picked_days():
    """Only a restricted window carries a day list; 24-hour times are kept."""
    rows = run_windows_probe(read("static", "app.js"))
    assert rows[1][1] == (
        '<p class="muted small live-windows">'
        "Allowed 08:00–09:00 (Mon, Tue, Wed, Thu, Fri) · 10:00–11:00 (Sat, Sun)</p>"
    )
    # a window on every day carries no day qualifier at all
    assert rows[0][1] == (
        '<p class="muted small live-windows">Allowed 14:00–15:00 · 18:00–19:00</p>'
    )
    assert i18n.STRINGS["en"]["card.day_mon"] == "Mon"
    assert i18n.STRINGS["en"]["card.window_days_all"] == "(Mon–Sun)"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_windows_line_is_absent_without_windows():
    """No windows means always open: no line, no empty paragraph."""
    js = read("static", "app.js")
    rows = run_windows_probe(js)
    assert rows[2][1] == ""
    assert rows[2][0] == '<p class="live-line ok">Open all day</p>'
    render = js_function(js, "renderToday")
    assert "liveHtml(group) + windowsLineHtml(group)" in render


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_windows_line_escapes_server_text():
    """A hostile window time cannot inject markup into the card."""
    rows = run_windows_probe(read("static", "app.js"))
    line = rows[4][1]
    assert "<script>" not in line
    assert "&lt;script&gt;" in line
    # escaped once, never twice
    assert "&amp;lt;" not in line
    assert "&amp;quot;" not in line
    # only the paragraph's own opening and closing tags may be real markup
    assert line.count("<") == 2


def signature_probe_script(js: str) -> str:
    """signatureFor with the real modeFor and window token, so edits repaint."""
    base = {
        "id": 7,
        "name": "Kids",
        "status": "allowed",
        "paused": False,
        "free_until": None,
        "force_block_until": 0,
    }
    cases = [
        dict(base, windows=[{"start": "14:00", "end": "15:00", "days": "all"}]),
        dict(base, windows=[{"start": "14:00", "end": "16:00", "days": "all"}]),
        dict(base, windows=[{"start": "14:00", "end": "15:00", "days": "sat,sun"}]),
        dict(base, windows=[{"start": "14:00", "end": "15:00", "days": "all"}]),
        dict(base, windows=[]),
    ]
    return "".join(
        [
            js_function(js, "modeFor"),
            js_function(js, "windowsKey"),
            js_function(js, "signatureFor"),
            "var cases = " + json.dumps(cases) + ";",
            "console.log(JSON.stringify(cases.map(signatureFor)));",
        ]
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_editing_a_window_changes_the_card_signature():
    """An edited window must repaint the card, and an unchanged one must not."""
    js = read("static", "app.js")
    done = subprocess.run(
        [shutil.which("node"), "-e", signature_probe_script(js)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    first, later_end, other_days, same_as_first, no_windows = json.loads(done.stdout)
    assert first and isinstance(first, str)
    assert first != later_end
    assert first != other_days
    assert first == same_as_first
    assert first != no_windows
    for gone in ("Date.now()", "minutes"):
        assert gone not in js_function(js, "signatureFor"), gone


def test_the_windows_line_reuses_the_muted_small_text():
    """Small, muted, wrapping: no new colour, no horizontal scroll."""
    js = read("static", "app.js")
    line = js_function(js, "windowsLineHtml")
    # only t(), esc() and the payload: no clock maths, no markup from the server
    for token in ("esc(", 't("card.windows_line"', "group.windows", " · "):
        assert token in line, token
    for gone in ("Date.now()", "innerHTML", "style=", "<script"):
        assert gone not in line, gone
    css = css_text()
    rule_text = rule(css, ".live-windows")
    assert "overflow-wrap: anywhere" in rule_text
    assert 'class="muted small live-windows"' in line
    assert "color: var(--muted)" in rule(css, ".muted")
    assert "font-size: 13px" in rule(css, ".small")


def test_the_windows_line_strings_are_shipped():
    for key in WINDOWS_KEYS:
        assert i18n.t(key) != key, key
    assert "{times}" in i18n.STRINGS["en"]["card.windows_line"]
    assert "{start}" in i18n.STRINGS["en"]["card.window_range"]
    assert "{end}" in i18n.STRINGS["en"]["card.window_range"]
    assert "–" in i18n.STRINGS["en"]["card.window_range"]
    assert "Allowed" in i18n.STRINGS["en"]["card.windows_line"]


def kid_probe_script(js: str) -> str:
    """bigLine with the real strings, so node can prove the kid text."""
    keys = (
        "kid.open_until",
        "kid.open_all_day",
        "kid.opens_at",
        "kid.free_until",
        "kid.blocked",
        "kid.paused",
    )
    cases = [
        {"status": "allowed", "open_until": "21:00", "opens_at": None},
        {"status": "allowed", "open_until": None, "opens_at": None},
        {"status": "outside_hours", "open_until": None, "opens_at": "14:00"},
        {"status": "free", "free_until": 1999999999},
        {"status": "blocked_admin"},
        {"status": "paused", "paused": True},
    ]
    return "".join(
        [
            "var i18n = " + json.dumps({key: i18n.STRINGS["en"][key] for key in keys}) + ";",
            "function t(key, vars) {",
            r"  return String(i18n[key] || key).replace(/\{(\w+)\}/g, function (whole, name) {",
            "    return vars && Object.prototype.hasOwnProperty.call(vars, name)",
            "      ? String(vars[name]) : whole;",
            "  });",
            "}",
            'function clock() { return "07:30"; }',
            js_function(js, "bigLine"),
            "var cases = " + json.dumps(cases) + ";",
            "console.log(JSON.stringify(cases.map(bigLine)));",
        ]
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_kid_line_follows_the_schedule_from_the_reduced_payload():
    js = read("static", "kid.js")
    done = subprocess.run(
        [shutil.which("node"), "-e", kid_probe_script(js)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [
        "Open until 21:00",
        "Open all day",
        "Opens at 14:00",
        "Free time until 07:30",
        "Blocked by a parent",
        "Paused by a parent",
    ]


def test_the_kid_page_reads_only_the_reduced_payload_keys():
    """kid.js runs against USER_GROUP_KEYS; a wider read would show undefined."""
    js = read("static", "kid.js")
    allowed = set(user_group_keys())
    fields = set(re.findall(r"\bgroup\.([a-z_]+)", js))
    assert fields
    assert fields <= allowed, sorted(fields - allowed)
    # and the removed fields are not even mentioned in passing
    for gone in ("used_minutes", "bonus_minutes", "bedtime_until", "limit_reached"):
        assert gone not in js, gone
    # the page draws one line per group: no progress bar, no used-today line
    assert "<progress" not in js
    assert "progressFor" not in js
    assert "kid.used_today" not in js


def test_the_group_list_names_the_allowed_times():
    """The list shows the schedule in words, never the removed minute columns."""
    js = read("static", "app.js")
    summary = js_function(js, "windowsSummary")
    assert 't("groups.windows_none")' in summary
    assert 't(count === 1 ? "groups.windows_one" : "groups.windows_other"' in summary
    render = js_function(js, "renderGroupList")
    assert "windowsSummary(g)" in render
    for gone in ("weekday_minutes", "weekend_minutes", "common.minutes"):
        assert gone not in render, gone
    for key in ("groups.windows_none", "groups.windows_one", "groups.windows_other"):
        assert i18n.t(key) != key, key
    assert "{count}" in i18n.STRINGS["en"]["groups.windows_one"]
    assert "{count}" in i18n.STRINGS["en"]["groups.windows_other"]


def test_every_i18n_key_the_ui_asks_for_exists():
    """Both scripts and the templates read only keys that are shipped."""
    # a trailing dot is a prefix the code completes at runtime, never a key
    prefixes: set[str] = set()
    for name in ("app.js", "kid.js"):
        text = read("static", name)
        keys = set(re.findall(r"""\bt\(\s*["']([^"']+)["']""", text))
        assert keys
        prefixes.update(key for key in keys if key.endswith("."))
        missing = sorted(
            key for key in keys if not key.endswith(".") and key not in i18n.STRINGS["en"]
        )
        assert missing == [], (name, missing)
    for path in sorted(package_file("templates").glob("*.html")):
        text = path.read_text(encoding="utf-8")
        keys = set(re.findall(r"""t\(\s*["']([^"']+)["']""", text))
        missing = sorted(
            key for key in keys if not key.endswith(".") and key not in i18n.STRINGS["en"]
        )
        assert missing == [], (path.name, missing)
    assert prefixes == {"users.role.", "warn."}
    for prefix in prefixes:
        # every value the code can append is a real key
        assert [key for key in i18n.STRINGS["en"] if key.startswith(prefix)]
