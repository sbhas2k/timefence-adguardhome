"""Allowed windows: membership, validation and the status they produce.

A window now means "the group may be used here"; no window at all means the
group is always allowed, and windows are the only schedule.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from timefence import budget, db

TZ = ZoneInfo("UTC")
BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture()
def store(tmp_path):
    store = db.Store(tmp_path / "w.db")
    with store as conn:
        db.migrate(conn, TZ)
    yield store
    store.close()


def ts_utc(year, month, day, hour, minute=0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp())


def ts_berlin(year, month, day, hour, minute=0, fold=0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=BERLIN, fold=fold).timestamp())


def a_window(start: str, end: str, days: str = "all") -> dict:
    return {"start": start, "end": end, "days": days}


def make_group(conn, *, windows=(), name="YouTube", services=("youtube",), domains=()):
    return budget.create_group(
        conn,
        name=name,
        services=list(services),
        domains=list(domains),
        windows=list(windows),
    )


# --------------------------------------------------------------------------- #
# membership
# --------------------------------------------------------------------------- #


def test_a_window_can_wrap_midnight(store):
    with store as conn:
        group = make_group(conn, windows=[a_window("21:00", "07:00")])
        assert budget.active_window(group, ts_utc(2026, 11, 11, 20, 59), TZ) is None
        assert budget.active_window(group, ts_utc(2026, 11, 11, 21, 0), TZ) is not None
        assert budget.active_window(group, ts_utc(2026, 11, 11, 23, 59), TZ) is not None
        # the morning half belongs to the day the window started
        assert budget.active_window(group, ts_utc(2026, 11, 12, 0, 0), TZ) is not None
        assert budget.active_window(group, ts_utc(2026, 11, 12, 6, 59), TZ) is not None
        assert budget.active_window(group, ts_utc(2026, 11, 12, 7, 0), TZ) is None


def test_a_window_within_one_day(store):
    with store as conn:
        group = make_group(conn, windows=[a_window("13:00", "14:30")])
        assert budget.active_window(group, ts_utc(2026, 11, 11, 12, 59), TZ) is None
        assert budget.active_window(group, ts_utc(2026, 11, 11, 13, 0), TZ) is not None
        assert budget.active_window(group, ts_utc(2026, 11, 11, 14, 29), TZ) is not None
        assert budget.active_window(group, ts_utc(2026, 11, 11, 14, 30), TZ) is None


def test_days_are_judged_by_the_day_the_window_starts(store):
    with store as conn:
        group = make_group(conn, windows=[a_window("21:00", "07:00", days="mon,tue,wed,thu,fri")])

        friday = ts_utc(2026, 11, 13, 22)  # a Friday
        assert datetime.fromtimestamp(friday, TZ).weekday() == 4
        assert budget.active_window(group, friday, TZ) is not None
        # Saturday morning belongs to Friday's window
        assert budget.active_window(group, ts_utc(2026, 11, 14, 2), TZ) is not None
        # Saturday night belongs to Saturday, which is not in the list
        assert budget.active_window(group, ts_utc(2026, 11, 14, 22), TZ) is None
        assert budget.active_window(group, ts_utc(2026, 11, 15, 2), TZ) is None


def test_whichever_days_are_listed_start_a_window(store):
    with store as conn:
        group = make_group(conn, windows=[a_window("21:00", "07:00", days="sat,sun")])
        assert budget.active_window(group, ts_utc(2026, 11, 13, 22), TZ) is None  # Friday
        assert budget.active_window(group, ts_utc(2026, 11, 14, 22), TZ) is not None  # Saturday
        assert budget.active_window(group, ts_utc(2026, 11, 15, 2), TZ) is not None  # Saturday's
        assert budget.active_window(group, ts_utc(2026, 11, 16, 2), TZ) is not None  # Sunday's
        assert budget.active_window(group, ts_utc(2026, 11, 16, 9), TZ) is None


def test_a_single_picked_day_opens_one_evening_that_week(store):
    """The point of the feature: a window on Mon and Fri but no day in between."""
    with store as conn:
        group = make_group(conn, windows=[a_window("16:00", "18:00", days="mon,fri")])
        for day in (10, 11, 12):  # Tue..Thu of that week
            assert budget.active_window(group, ts_utc(2026, 11, day, 17), TZ) is None
        assert budget.active_window(group, ts_utc(2026, 11, 9, 17), TZ) is not None  # Monday
        assert budget.active_window(group, ts_utc(2026, 11, 13, 17), TZ) is not None  # Friday
        assert budget.active_window(group, ts_utc(2026, 11, 14, 17), TZ) is None  # Saturday


def test_overlapping_windows_are_a_union(store):
    with store as conn:
        group = make_group(
            conn,
            windows=[a_window("20:00", "22:00"), a_window("21:00", "07:00", days="sat,sun")],
        )
        assert budget.active_window(group, ts_utc(2026, 11, 11, 20, 30), TZ) is not None
        assert budget.active_window(group, ts_utc(2026, 11, 11, 23, 30), TZ) is None
        assert budget.active_window(group, ts_utc(2026, 11, 14, 23, 30), TZ) is not None


def test_a_window_over_a_dst_change_uses_the_wall_clock(store):
    # Berlin: 2026-03-29 02:00 CET jumps to 03:00 CEST, so 02:30 never happens.
    skipped = int(datetime(2026, 3, 29, 2, 30, tzinfo=BERLIN).timestamp())
    assert datetime.fromtimestamp(skipped, BERLIN).strftime("%H:%M") == "03:30"

    with store as conn:
        group = make_group(conn, windows=[a_window("01:00", "02:30")])
        assert budget.active_window(group, ts_berlin(2026, 3, 29, 1, 45), BERLIN) is not None
        # the window ends at the transition instant, not at a resolved 03:30
        end = budget.schedule_state(group, BERLIN, ts_berlin(2026, 3, 29, 1, 0))[1]
        assert end is not None
        assert datetime.fromtimestamp(end, BERLIN).strftime("%H:%M") == "03:00"
        assert end < skipped


def test_a_fall_back_window_stays_open_until_the_second_end_time(store):
    # Berlin: 2026-10-25 03:00 CEST falls back to 02:00 CET, so 02:xx repeats.
    first_pass = ts_berlin(2026, 10, 25, 2, 15, fold=0)
    second_pass = ts_berlin(2026, 10, 25, 2, 15, fold=1)
    assert second_pass - first_pass == 3600

    with store as conn:
        group = make_group(conn, windows=[a_window("01:00", "02:30")])
        assert budget.active_window(group, first_pass, BERLIN) is not None
        assert budget.active_window(group, second_pass, BERLIN) is not None
        end = budget.schedule_state(group, BERLIN, second_pass)[1]

    assert end == ts_berlin(2026, 10, 25, 2, 30, fold=1)
    assert budget.window_active(group.windows[0], end, BERLIN) is False


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


def test_windows_are_validated(store):
    with store as conn:
        for bad in (
            a_window("21:00", "21:00"),
            a_window("9:00", "07:00"),
            a_window("24:00", "07:00"),
            a_window("21:00", "07:0"),
            a_window("21:00", "07:00", days="daily"),
            a_window("21:00", "07:00", days="mon,"),
            a_window("21:00", "07:00", days="monday"),
            a_window("21:00", "07:00", days="mon;fri"),
        ):
            with pytest.raises(budget.ValidationError):
                make_group(conn, windows=[bad])


def test_the_day_list_is_normalised_to_week_order(store):
    """Any set of days is stored as one canonical string the schedule parses."""
    cases = [
        (None, "all"),
        ("", "all"),
        ("  ", "all"),
        ("all", "all"),
        ("ALL", "all"),
        ("mon", "mon"),
        ("fri,mon", "mon,fri"),
        ("fri,mon,fri", "mon,fri"),
        (" mon , fri ", "mon,fri"),
        ("sun,sat", "sat,sun"),
        ("wed,thu,tue,mon,fri,sat,sun", "all"),
    ]
    with store as conn:
        for index, (raw, expected) in enumerate(cases):
            window = {"start": "09:00", "end": "10:00"}
            if raw is not None:
                window["days"] = raw
            group = make_group(
                conn,
                name=f"Case {index}",
                services=[],
                domains=[f"case{index}.example"],
                windows=[window],
            )
            assert group.windows[0].days == expected, raw
            all_days = budget.WINDOW_DAYS_SHORT
            matches = [day for day in range(7) if budget.days_match(expected, day)]
            if expected == "all":
                assert matches == list(range(7))
            else:
                assert matches == [all_days.index(name) for name in expected.split(",")]


def test_windows_must_be_a_list_of_objects(store):
    with store as conn:
        for bad in ("21:00-07:00", {"start": "21:00"}, [5], [{"start": "21:00"}]):
            with pytest.raises(budget.ValidationError):
                make_group(conn, windows=bad)


def test_at_most_four_windows(store):
    four = [a_window("06:00", "07:00"), a_window("12:00", "13:00")]
    four += [a_window("15:00", "16:00"), a_window("18:00", "19:00")]
    with store as conn:
        group = make_group(conn, windows=four)
        assert len(group.windows) == 4
        with pytest.raises(budget.WindowError, match="at most 4 time windows"):
            make_group(
                conn,
                name="Other",
                services=("roblox",),
                windows=four + [a_window("21:00", "22:00")],
            )


def test_windows_are_replaced_and_cleared_on_update(store):
    with store as conn:
        group = make_group(conn, windows=[a_window("21:00", "07:00")])
        replaced = budget.update_group(
            conn, group.id, windows=[a_window("12:00", "13:00", days="sat,sun")]
        )
        assert [w.as_dict() for w in replaced.windows] == [
            {"start": "12:00", "end": "13:00", "days": "sat,sun"}
        ]
        kept = budget.update_group(conn, group.id, emoji="🎮")
        assert len(kept.windows) == 1
        cleared = budget.update_group(conn, group.id, windows=[])
        assert cleared.windows == []
        assert budget.get_group(conn, group.id).windows == []


# --------------------------------------------------------------------------- #
# status: windows are the allowed hours
# --------------------------------------------------------------------------- #


def test_no_windows_means_always_allowed(store):
    with store as conn:
        group = make_group(conn)
        moment = ts_utc(2026, 11, 11, 3)
        assert budget.schedule_state(group, TZ, moment) == (True, None)
        assert budget.group_status(conn, group, TZ, moment) == ("allowed", None)
        assert budget.is_permitted(conn, group, TZ, moment) is True


def test_outside_the_window_is_outside_hours_with_the_next_opening(store):
    with store as conn:
        group = make_group(conn, windows=[a_window("13:00", "14:30")])
        now = ts_utc(2026, 11, 11, 12)
        assert budget.group_status(conn, group, TZ, now) == (
            "outside_hours",
            ts_utc(2026, 11, 11, 13),
        )
        assert budget.is_permitted(conn, group, TZ, now) is False

        inside = ts_utc(2026, 11, 11, 13, 30)
        assert budget.group_status(conn, group, TZ, inside) == ("allowed", None)
        assert budget.is_permitted(conn, group, TZ, inside) is True


def test_group_state_reports_the_next_opening_and_closing(store):
    with store as conn:
        group = make_group(conn, windows=[a_window("13:00", "14:30")])
        closed = budget.group_state(conn, group, TZ, ts_utc(2026, 11, 11, 12))
        assert closed["status"] == "outside_hours"
        assert closed["open_now"] is False
        assert closed["opens_at"] == "13:00"
        assert closed["open_until"] is None
        assert closed["blocked_until"] == ts_utc(2026, 11, 11, 13)
        assert closed["next_change"] == ts_utc(2026, 11, 11, 13)

        opened = budget.group_state(conn, group, TZ, ts_utc(2026, 11, 11, 13, 10))
        assert opened["status"] == "allowed"
        assert opened["open_now"] is True
        assert opened["opens_at"] is None
        assert opened["open_until"] == "14:30"
        assert opened["next_change"] == ts_utc(2026, 11, 11, 14, 30)

        all_day = make_group(conn, name="Always", services=("roblox",))
        state = budget.group_state(conn, all_day, TZ, ts_utc(2026, 11, 11, 3))
        assert state["open_now"] is True
        assert state["open_until"] is None
        assert state["next_change"] is None


# --------------------------------------------------------------------------- #
# proof: every day set, every weekday, wrapping and not
# --------------------------------------------------------------------------- #

MONDAY = 9  # 2026-11-09 is a Monday (that week has no DST change in UTC)


def day_sets() -> list[str]:
    """All 128 subsets of the week plus the `all` sentinel, as stored strings."""
    out = [budget.WINDOW_DAYS_ALL]
    for size in range(1, len(budget.WINDOW_DAYS_SHORT) + 1):
        for combo in itertools.combinations(budget.WINDOW_DAYS_SHORT, size):
            out.append(",".join(combo))
    assert len(out) == 2 ** len(budget.WINDOW_DAYS_SHORT)
    return out


def oracle_active(start_min: int, end_min: int, days: str, ts: int, tz: ZoneInfo) -> bool:
    """Read the stored day list and the wall clock independently of budget.py."""
    names = budget.WINDOW_DAYS_SHORT
    chosen = set(names) if days == budget.WINDOW_DAYS_ALL else set(days.split(","))
    local = datetime.fromtimestamp(ts, tz)
    minute = local.hour * 60 + local.minute
    today = names[local.weekday()]
    yesterday = names[(local.date() - timedelta(days=1)).weekday()]
    if start_min < end_min:
        return today in chosen and start_min <= minute < end_min
    if today in chosen and minute >= start_min:
        return True
    return yesterday in chosen and minute < end_min


@pytest.mark.parametrize(
    ("start_min", "end_min", "offsets"),
    [
        # inside one day: just before, at the start, at the end, just after
        (13 * 60, 14 * 60 + 30, (-1, 0, 89, 90)),
        # across midnight: the evening half, then the morning half next day
        (21 * 60, 7 * 60, (-1, 0, 179, 180, 539, 540, 24 * 60)),
    ],
)
def test_window_active_agrees_with_the_day_list_oracle(start_min, end_min, offsets):
    """All 2048 day-set/day/shape combinations: the answer is the oracle's."""
    window = budget.Window(start_min, end_min, "all")
    for days in day_sets():
        current = budget.Window(start_min, end_min, days)
        for weekday in range(7):
            midnight = ts_utc(2026, 11, MONDAY + weekday, 0)
            for offset in offsets:
                moment = midnight + offset * 60
                expected = oracle_active(start_min, end_min, days, moment, TZ)
                assert budget.window_active(current, moment, TZ) is expected, (
                    days,
                    weekday,
                    offset,
                )
                # the normalised form must behave the same as the raw string
                assert budget.window_active(window, moment, TZ) == oracle_active(
                    start_min, end_min, "all", moment, TZ
                )
