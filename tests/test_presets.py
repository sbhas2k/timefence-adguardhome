"""Starter presets: valid windows, valid domains, admin-only, UI sugar only.

A preset fills the group editor; it never writes to the database or to AdGuard.
"""

from __future__ import annotations

from pathlib import Path

from timefence import budget, i18n
from timefence import presets as presets_module

from .fakes import load_service_catalogue
from .test_api import login, make_group


def preset_list() -> list[dict]:
    return presets_module.payload()


# --------------------------------------------------------------------------- #
# the data itself
# --------------------------------------------------------------------------- #


def test_there_are_exactly_the_two_shipped_presets():
    keys = [preset["key"] for preset in preset_list()]
    assert keys == ["school_day", "no_late_nights"]


def test_every_preset_has_a_name_and_a_bounded_window_list():
    for preset in preset_list():
        assert preset["name_key"].startswith("preset.")
        assert i18n.t(preset["name_key"]) != preset["name_key"], preset["key"]
        assert 1 <= len(preset["windows"]) <= budget.WINDOW_LIMIT
        assert isinstance(preset["domains"], list)
        assert set(preset) == {"key", "name_key", "windows", "domains"}


def test_every_preset_window_passes_the_api_validator():
    """The same rule the group editor's payload hits, not a looser one."""
    for preset in preset_list():
        for window in preset["windows"]:
            assert set(window) == {"start", "end", "days"}
            validated = budget.normalize_windows([window])
            assert len(validated) == 1
            # the stored form is the canonical one, so the UI never reformats it
            assert validated[0].as_dict() == window
            assert (
                window["days"]
                in (
                    budget.WINDOW_DAYS_ALL,
                    ",".join(budget.WINDOW_DAYS_SHORT),
                )
                or budget.normalize_days(window["days"]) == window["days"]
            )


def test_the_night_preset_really_crosses_midnight():
    nights = next(p for p in preset_list() if p["key"] == "no_late_nights")
    for window in nights["windows"]:
        start = budget.clock_minutes(window["start"])
        end = budget.clock_minutes(window["end"])
        assert start > end, window


def test_every_preset_domain_passes_the_domains_validator():
    """A shipped domain must be one the editor's own field would accept."""
    for preset in preset_list():
        for domain in preset["domains"]:
            assert budget.normalize_domain(domain) == domain
            assert domain == domain.strip().lower()


def test_a_preset_never_names_a_service_id():
    """Presets carry domains, not AdGuard service ids, so any catalogue works."""
    catalogue = {service["id"] for service in load_service_catalogue()}
    for preset in preset_list():
        for domain in preset["domains"]:
            assert domain not in catalogue, domain
        assert not set(preset) & {"services", "service_ids"}


def test_the_presets_module_cannot_write_anything():
    """It is a constant table: no database, no AdGuard, no file side effects."""
    source = Path(presets_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("import sqlite3", "from . import db", "from .db", "adguard", "open("):
        assert forbidden not in source, forbidden


# --------------------------------------------------------------------------- #
# the API
# --------------------------------------------------------------------------- #


def test_get_presets_returns_both_for_an_admin(ready):
    client = ready["client"]
    response = client.get("/api/presets")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [preset["key"] for preset in body["presets"]] == ["school_day", "no_late_nights"]
    assert body["presets"][0]["windows"][0]["days"] == "mon,tue,wed,thu,fri"


def test_get_presets_is_refused_for_a_user_account(ready):
    client = ready["client"]
    created = client.post(
        "/api/users", json={"username": "kid", "password": "kid-password", "role": "user"}
    )
    assert created.status_code == 200, created.text
    make_group(client)
    login(client, "kid", "kid-password")
    response = client.get("/api/presets")
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# the editor wiring
# --------------------------------------------------------------------------- #


def package_source(*parts: str) -> str:
    from timefence import api

    return Path(api.__file__).parent.joinpath(*parts).read_text(encoding="utf-8")


def test_the_editor_fills_the_domains_and_the_windows_from_a_preset():
    js = package_source("static", "app.js")
    editor = js[js.index("function openGroupEditorWith(") : js.index("function openServicePicker(")]
    assert 'data-action="apply-preset"' in editor
    assert 'data-preset="' in editor
    assert 't("preset.apply")' in editor
    # only a new group: an existing schedule is never overwritten by a preset
    assert 'if (group.id || !presets.length) { return ""; }' in editor
    handler = editor[editor.index("function applyPreset(key)") : editor.index("function sheet()")]
    assert "(preset.windows || []).map(" in handler
    assert "windows = " in handler
    assert "paintWindows();" in handler
    assert 'var domains = $("#g-domains");' in handler
    assert "domains.value = preset.domains.join" in handler
    # the buttons in the row call it with the preset the server named
    bound = editor[editor.index("$$(\"[data-action='apply-preset']\")") :]
    assert "applyPreset(button.dataset.preset);" in bound
    # presets come from the server, never hard-coded in the script
    loader = js[js.index("function openGroupEditor(") : js.index("function openGroupEditorWith(")]
    assert "presets" in loader
    assert "/api/presets" in js
