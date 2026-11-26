"""Starter schedules the group editor can apply in one tap.

A preset is read-only UI sugar: it fills the domains field and the allowed-times
editor of a new group. Nothing here writes to the database or to AdGuard Home,
so a preset can never change a schedule behind the admin's back.

The windows use the same day strings the API validates: the `all` sentinel or a
comma-separated list in week order (`mon,tue,wed,thu,fri`).

Domain lists are deliberately empty for 1.1.0. The host lists that would make
these presets useful name services such as video and game sites, and none of
them could be checked against a real AdGuard service catalogue with this
release, so the presets carry windows only and the group's own domains field
stays the source of targets. Shipping an unverified list would quietly block
nothing (or the wrong things), which is worse than an empty one.
"""

from __future__ import annotations

PRESETS: dict[str, dict] = {
    "school_day": {
        "name_key": "preset.school_day",
        "windows": [
            {"start": "09:00", "end": "17:00", "days": "mon,tue,wed,thu,fri"},
            {"start": "09:00", "end": "18:00", "days": "sat,sun"},
        ],
        "domains": [],
    },
    "no_late_nights": {
        "name_key": "preset.no_late_nights",
        "windows": [
            # the evening window runs into the next morning, which the day list
            # of the day it starts on now expresses directly
            {"start": "18:00", "end": "01:00", "days": "mon,tue,wed,thu,fri"},
            {"start": "17:00", "end": "01:00", "days": "sat,sun"},
        ],
        "domains": [],
    },
}


def payload() -> list[dict]:
    """The wire form of every preset, in the order the editor shows them."""
    return [{"key": key, **preset} for key, preset in PRESETS.items()]
