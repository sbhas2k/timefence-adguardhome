"""The allowed schedule: open now, and the exact instant that changes.

The fast candidate-edge path is proved against a brute-force minute-stepping
oracle for random windows in five timezones (including DST transitions), and a
timing guard keeps the 30 s state poll cheap.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from timefence import budget
from timefence.budget import Group, Window

ZONES = (
    "UTC",
    "Europe/London",
    "America/New_York",
    "Asia/Kolkata",
    "Australia/Lord_Howe",
)


def a_group(windows) -> Group:
    return Group(
        id=1,
        name="Games",
        emoji="",
        paused=False,
        deleted=False,
        windows=list(windows),
    )


def ts(year, month, day, hour, minute=0, tz=ZoneInfo("UTC"), fold=0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=tz, fold=fold).timestamp())


def oracle(windows, tz, now) -> tuple[bool, int | None]:
    """Minute by minute: the answer the fast path must reproduce exactly."""
    if not windows:
        return True, None
    current = any(budget.window_active(window, now, tz) for window in windows)
    instant = now - now % 60
    for _ in range(8 * 1440):
        instant += 60
        if any(budget.window_active(window, instant, tz) for window in windows) != current:
            return current, instant
    return current, None


# --------------------------------------------------------------------------- #
# plain schedules
# --------------------------------------------------------------------------- #


def test_no_windows_is_always_open_and_never_flips():
    assert budget.schedule_state(a_group([]), ZoneInfo("UTC"), 1_800_000_000) == (True, None)


def test_a_window_within_one_day_reports_both_edges():
    tz = ZoneInfo("UTC")
    group = a_group([Window(13 * 60, 14 * 60 + 30)])
    assert budget.schedule_state(group, tz, ts(2026, 11, 11, 12)) == (
        False,
        ts(2026, 11, 11, 13),
    )
    assert budget.schedule_state(group, tz, ts(2026, 11, 11, 13)) == (
        True,
        ts(2026, 11, 11, 14, 30),
    )
    assert budget.schedule_state(group, tz, ts(2026, 11, 11, 14, 30)) == (
        False,
        ts(2026, 11, 12, 13),
    )


def test_the_start_is_inclusive_and_the_end_exclusive():
    tz = ZoneInfo("UTC")
    group = a_group([Window(13 * 60, 14 * 60)])
    assert budget.window_active(group.windows[0], ts(2026, 11, 11, 12, 59), tz) is False
    assert budget.window_active(group.windows[0], ts(2026, 11, 11, 13, 0), tz) is True
    assert budget.window_active(group.windows[0], ts(2026, 11, 11, 13, 59), tz) is True
    assert budget.window_active(group.windows[0], ts(2026, 11, 11, 14, 0), tz) is False


def test_a_window_wrapping_midnight_closes_on_the_morning_after():
    tz = ZoneInfo("UTC")
    group = a_group([Window(22 * 60, 7 * 60)])
    assert budget.schedule_state(group, tz, ts(2026, 11, 11, 21)) == (False, ts(2026, 11, 11, 22))
    assert budget.schedule_state(group, tz, ts(2026, 11, 11, 23)) == (True, ts(2026, 11, 12, 7))
    assert budget.schedule_state(group, tz, ts(2026, 11, 12, 8)) == (False, ts(2026, 11, 12, 22))


def test_the_day_filter_belongs_to_the_day_the_window_starts():
    tz = ZoneInfo("UTC")
    group = a_group([Window(22 * 60, 7 * 60, "mon,tue,wed,thu,fri")])
    # Friday 22:00 opens and stays open into Saturday morning
    assert budget.schedule_state(group, tz, ts(2026, 11, 13, 21)) == (False, ts(2026, 11, 13, 22))
    assert budget.schedule_state(group, tz, ts(2026, 11, 14, 2)) == (True, ts(2026, 11, 14, 7))
    # the weekend nights do not start a window; the next one is Monday evening
    assert budget.schedule_state(group, tz, ts(2026, 11, 14, 8)) == (False, ts(2026, 11, 16, 22))


def test_a_weekend_only_window_reaches_five_days_ahead():
    tz = ZoneInfo("UTC")
    group = a_group([Window(20 * 60, 23 * 60, "sat,sun")])
    monday = ts(2026, 11, 16, 8)
    assert budget.schedule_state(group, tz, monday) == (False, ts(2026, 11, 21, 20))


def test_two_windows_are_a_union():
    tz = ZoneInfo("UTC")
    group = a_group([Window(8 * 60, 9 * 60), Window(17 * 60, 19 * 60)])
    assert budget.schedule_state(group, tz, ts(2026, 11, 11, 8, 30)) == (
        True,
        ts(2026, 11, 11, 9),
    )
    assert budget.schedule_state(group, tz, ts(2026, 11, 11, 9)) == (False, ts(2026, 11, 11, 17))
    assert budget.schedule_state(group, tz, ts(2026, 11, 11, 18)) == (
        True,
        ts(2026, 11, 11, 19),
    )


def test_windows_that_cover_the_whole_day_never_flip():
    group = a_group([Window(0, 12 * 60), Window(12 * 60, 0)])
    tz = ZoneInfo("UTC")
    assert budget.schedule_state(group, tz, ts(2026, 11, 11, 0)) == (True, None)
    assert budget.schedule_state(group, tz, ts(2026, 11, 11, 23, 59)) == (True, None)


# --------------------------------------------------------------------------- #
# daylight saving: the wall clock is not always continuous
# --------------------------------------------------------------------------- #

LONDON = ZoneInfo("Europe/London")
NY = ZoneInfo("America/New_York")


def test_a_window_spanning_the_spring_gap_closes_at_the_new_wall_time():
    # London, 2026-03-29: 01:00 GMT jumps to 02:00 BST, so 02:30 is 01:30 UTC.
    group = a_group([Window(30, 2 * 60 + 30)])
    start_of_day = ts(2026, 3, 29, 0, tz=LONDON)
    assert budget.schedule_state(group, LONDON, start_of_day) == (
        False,
        ts(2026, 3, 29, 0, 30, tz=LONDON),
    )
    inside = ts(2026, 3, 29, 1, 15)  # 01:15 UTC, 02:15 BST
    # 02:30 on that day is 01:30 UTC, not the nonexistent wall time built with
    # the pre-transition offset
    assert budget.schedule_state(group, LONDON, inside) == (True, ts(2026, 3, 29, 1, 30))


def test_a_window_in_the_spring_gap_simply_never_opens():
    # New York, 2026-03-08: 02:00 EST jumps to 03:00 EDT; 02:xx never happens.
    group = a_group([Window(2 * 60, 3 * 60)])
    early = ts(2026, 3, 8, 0, tz=NY)
    assert budget.schedule_state(group, NY, early) == (False, ts(2026, 3, 9, 2, tz=NY))
    for minute in (100, 110, 120, 130, 140, 150, 160):
        moment = ts(2026, 3, 8, 0, tz=NY) + minute * 60
        assert budget.window_active(group.windows[0], moment, NY) is False


def test_a_window_in_the_fall_back_hour_opens_twice():
    # New York, 2026-11-01: 02:00 EDT falls back to 01:00 EST, so 01:xx repeats.
    group = a_group([Window(1 * 60, 1 * 60 + 30)])
    first_pass = ts(2026, 11, 1, 5, 10)  # 01:10 EDT
    assert budget.schedule_state(group, NY, first_pass) == (True, ts(2026, 11, 1, 5, 30))
    between = ts(2026, 11, 1, 5, 40)  # 01:40 EDT, after the first closing
    assert budget.schedule_state(group, NY, between) == (False, ts(2026, 11, 1, 6, 0))
    second_pass = ts(2026, 11, 1, 6, 10)  # 01:10 EST
    assert budget.schedule_state(group, NY, second_pass) == (True, ts(2026, 11, 1, 6, 30))


def test_a_window_over_the_fall_back_hour_stays_open_through_it():
    # Berlin, 2026-10-25: 03:00 CEST falls back to 02:00 CET; 02:00-03:00 local
    # covers two hours of real time with no instant in between that is closed.
    berlin = ZoneInfo("Europe/Berlin")
    group = a_group([Window(2 * 60, 3 * 60)])
    now = ts(2026, 10, 25, 0, 30, tz=berlin)  # 00:30 CEST
    assert budget.schedule_state(group, berlin, now) == (False, ts(2026, 10, 25, 2, tz=berlin))
    during = ts(2026, 10, 25, 2, 30, tz=berlin, fold=0)  # 02:30 CEST
    assert budget.schedule_state(group, berlin, during) == (True, ts(2026, 10, 25, 3, tz=berlin))


# --------------------------------------------------------------------------- #
# proof: the fast path against the oracle
# --------------------------------------------------------------------------- #


def random_days(rng: random.Random) -> str:
    """`all` or any set of days, the way the editor can now produce them."""
    if rng.random() < 0.25:
        return budget.WINDOW_DAYS_ALL
    picked = [day for day in budget.WINDOW_DAYS_SHORT if rng.random() < 0.5]
    return budget.normalize_days(",".join(picked) or "all")


def random_windows(rng: random.Random) -> list[Window]:
    windows = []
    for _ in range(rng.randint(0, 4)):
        start = rng.randrange(0, 1440)
        end = rng.randrange(0, 1439)
        if end >= start:
            end += 1
        windows.append(Window(start, end, random_days(rng)))
    return windows


@pytest.mark.parametrize("zone", ZONES)
def test_the_fast_path_matches_the_minute_oracle(zone):
    tz = ZoneInfo(zone)
    rng = random.Random(f"timefence-schedule-{zone}")
    checked = 0
    for _ in range(15):
        windows = random_windows(rng)
        base = datetime(2026, 3, 1, tzinfo=tz) + timedelta(days=rng.randrange(0, 300))
        now = int(base.timestamp()) + rng.randrange(0, 86400) + rng.randrange(0, 60)
        expected = oracle(windows, tz, now)
        assert budget.schedule_state(a_group(windows), tz, now) == expected, (
            zone,
            now,
            [window.as_dict() for window in windows],
        )
        checked += 1
    assert checked == 15


@pytest.mark.parametrize("zone", ("UTC", "Europe/London", "America/New_York"))
def test_the_fast_path_matches_the_oracle_across_the_transition(zone):
    """The hours around a real DST change, minute for minute."""
    tz = ZoneInfo(zone)
    transitions = {
        "UTC": [(2026, 3, 29), (2026, 10, 25)],
        "Europe/London": [(2026, 3, 29), (2026, 10, 25)],
        "America/New_York": [(2026, 3, 8), (2026, 11, 1)],
    }[zone]
    rng = random.Random(f"timefence-transition-{zone}")
    for day in transitions:
        midnight = ts(*day, 0, tz=tz)
        for window in (
            [Window(0, 3 * 60)],
            [Window(1 * 60, 2 * 60 + 30)],
            [Window(2 * 60, 3 * 60)],
            [Window(23 * 60, 2 * 60)],
            random_windows(rng),
        ):
            for offset in range(-2 * 3600, 4 * 3600, 1237):
                now = midnight + offset
                assert budget.schedule_state(a_group(window), tz, now) == oracle(
                    list(window), tz, now
                ), (zone, day, now, [w.as_dict() for w in window])


def test_the_state_poll_stays_fast():
    """Four windows must not turn a 30 s poll into a busy loop (< 5 ms)."""
    tz = ZoneInfo("UTC")
    group = a_group([Window(60, 120), Window(360, 420), Window(720, 780), Window(1080, 1140)])
    now = ts(2026, 11, 11, 13)
    budget.schedule_state(group, tz, now)  # warm the interpreter up
    started = time.perf_counter()
    for _ in range(50):
        budget.schedule_state(group, tz, now)
    average = (time.perf_counter() - started) / 50
    assert average < 0.005, f"schedule_state took {average * 1000:.2f} ms"
