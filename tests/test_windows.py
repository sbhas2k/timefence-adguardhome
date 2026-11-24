"""Allowed windows: membership, validation and the status they produce.

A window now means "the group may be used here"; no window at all means the
group is always allowed, and windows are the only schedule.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
        group = make_group(conn, windows=[a_window("21:00", "07:00", days="weekday")])

        friday = ts_utc(2026, 11, 13, 22)  # a Friday
        assert datetime.fromtimestamp(friday, TZ).weekday() == 4
        assert budget.active_window(group, friday, TZ) is not None
        # Saturday morning belongs to Friday's window
        assert budget.active_window(group, ts_utc(2026, 11, 14, 2), TZ) is not None
        # Saturday night belongs to Saturday, which is not a weekday
        assert budget.active_window(group, ts_utc(2026, 11, 14, 22), TZ) is None
        assert budget.active_window(group, ts_utc(2026, 11, 15, 2), TZ) is None


def test_weekend_windows_start_on_saturday_and_sunday(store):
    with store as conn:
        group = make_group(conn, windows=[a_window("21:00", "07:00", days="weekend")])
        assert budget.active_window(group, ts_utc(2026, 11, 13, 22), TZ) is None  # Friday
        assert budget.active_window(group, ts_utc(2026, 11, 14, 22), TZ) is not None  # Saturday
        assert budget.active_window(group, ts_utc(2026, 11, 15, 2), TZ) is not None  # Saturday's
        assert budget.active_window(group, ts_utc(2026, 11, 16, 2), TZ) is not None  # Sunday's
        assert budget.active_window(group, ts_utc(2026, 11, 16, 9), TZ) is None


def test_overlapping_windows_are_a_union(store):
    with store as conn:
        group = make_group(
            conn,
            windows=[a_window("20:00", "22:00"), a_window("21:00", "07:00", days="weekend")],
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
        ):
            with pytest.raises(budget.ValidationError):
                make_group(conn, windows=[bad])


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
            conn, group.id, windows=[a_window("12:00", "13:00", days="weekend")]
        )
        assert [w.as_dict() for w in replaced.windows] == [
            {"start": "12:00", "end": "13:00", "days": "weekend"}
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
