"""Bedtime windows: membership, status, metering and the AdGuard state."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from timefence import budget, db
from timefence.reconciler import BUCKET, Reconciler

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


def make_group(conn, *, windows=(), minutes=60, name="YouTube", services=("youtube",), domains=()):
    return budget.create_group(
        conn,
        name=name,
        emoji="",
        weekday_minutes=minutes,
        weekend_minutes=minutes,
        services=list(services),
        domains=list(domains),
        windows=list(windows),
    )


def stamp(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S")


def entry(ts: int, host: str = "www.youtube.com") -> dict:
    return {
        "time": f"{stamp(ts)}.889389948Z",
        "client": "192.168.1.20",
        "reason": "NotFilteredNotFound",
        "question": {"name": host, "type": "A"},
    }


def snapshot(store, group_id, now, tz=TZ):
    """Everything the API reports about one group, computed once."""
    with store as conn:
        group = budget.get_group(conn, group_id)
        state = budget.group_state(conn, group, tz, now)
        permitted = budget.is_permitted(conn, group, tz, now)
        status, blocked_until = budget.group_status(conn, group, tz, now)
    return state, permitted, status, blocked_until


def assert_agree(state, permitted, status):
    assert state["status"] == status
    assert state["permitted"] == permitted
    assert permitted == (status in {"allowed", "free"})


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

        active, end = budget.bedtime_state(conn, group, TZ, ts_utc(2026, 11, 11, 22))
        assert active is True
        assert end == ts_utc(2026, 11, 12, 7)
        assert budget.bedtime_state(conn, group, TZ, ts_utc(2026, 11, 12, 8)) == (False, None)


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


# --------------------------------------------------------------------------- #
# daylight-saving days stay on the local wall clock
# --------------------------------------------------------------------------- #


def test_spring_forward_window_ends_at_the_transition(store):
    # Berlin: 2026-03-29 02:00 CET jumps to 03:00 CEST, so 02:30 never happens.
    skipped = int(datetime(2026, 3, 29, 2, 30, tzinfo=BERLIN).timestamp())
    assert datetime.fromtimestamp(skipped, BERLIN).strftime("%H:%M") == "03:30"

    with store as conn:
        group = make_group(conn, windows=[a_window("01:00", "02:30")])
        assert budget.active_window(group, ts_berlin(2026, 3, 29, 1, 45), BERLIN) is not None
        active, end = budget.bedtime_state(conn, group, BERLIN, ts_berlin(2026, 3, 29, 1, 0))

    assert active is True
    # the window ends at the transition instant, not at a silently resolved 03:30
    assert end == ts_berlin(2026, 3, 29, 3, 0)
    assert datetime.fromtimestamp(end, BERLIN).strftime("%H:%M") == "03:00"
    assert end < skipped


def test_fall_back_window_stays_active_until_the_second_end_time(store):
    # Berlin: 2026-10-25 03:00 CEST falls back to 02:00 CET, so 02:xx repeats.
    first_pass = ts_berlin(2026, 10, 25, 2, 15, fold=0)
    second_pass = ts_berlin(2026, 10, 25, 2, 15, fold=1)
    assert second_pass - first_pass == 3600

    with store as conn:
        group = make_group(conn, windows=[a_window("01:00", "02:30")])
        assert budget.active_window(group, first_pass, BERLIN) is not None
        assert budget.active_window(group, second_pass, BERLIN) is not None
        active, end = budget.bedtime_state(conn, group, BERLIN, second_pass)

    assert active is True
    assert end == ts_berlin(2026, 10, 25, 2, 30, fold=1)
    assert budget.window_active(group.windows[0], end, BERLIN) is False


def test_a_midnight_window_reports_its_end_time(store):
    with store as conn:
        group = make_group(conn, windows=[a_window("21:00", "07:00")])
        state = budget.group_state(conn, group, TZ, ts_utc(2026, 11, 11, 23, 30))
    assert state["bedtime_active"] is True
    assert state["bedtime_until"] == "07:00"
    assert state["status"] == "bedtime"
    assert state["blocked_until"] == ts_utc(2026, 11, 12, 7)


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


def test_at_most_four_windows(store):
    four = [a_window("06:00", "07:00"), a_window("12:00", "13:00")]
    four += [a_window("15:00", "16:00"), a_window("18:00", "19:00")]
    with store as conn:
        group = make_group(conn, windows=four)
        assert len(group.windows) == 4
        with pytest.raises(budget.ValidationError):
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
        kept = budget.update_group(conn, group.id, weekday_minutes=30)
        assert len(kept.windows) == 1
        cleared = budget.update_group(conn, group.id, windows=[])
        assert cleared.windows == []
        assert budget.get_group(conn, group.id).windows == []


# --------------------------------------------------------------------------- #
# status, precedence and metering
# --------------------------------------------------------------------------- #


def test_status_and_permitted_agree(store):
    evening = ts_utc(2026, 11, 11, 22)  # inside the bedtime window
    with store as conn:
        bedtime = make_group(conn, windows=[a_window("21:00", "07:00")], minutes=60)
        plain = make_group(conn, name="Plain", services=("roblox",), minutes=60)

    state, permitted, status, _ = snapshot(store, plain.id, evening)
    assert status == "allowed"
    assert_agree(state, permitted, status)

    state, permitted, status, _ = snapshot(store, bedtime.id, evening)
    assert status == "bedtime"
    assert_agree(state, permitted, status)

    with store as conn:
        budget.add_metered_seconds(conn, plain.id, budget.day_str(evening, TZ), 3600, 3600)
    state, permitted, status, blocked_until = snapshot(store, plain.id, evening)
    assert status == "limit_reached"
    assert blocked_until == budget.next_local_midnight(evening, TZ)
    assert_agree(state, permitted, status)

    with store as conn:
        budget.set_force_block(conn, bedtime.id, evening + 600)
    state, permitted, status, blocked_until = snapshot(store, bedtime.id, evening)
    assert status == "blocked_admin"
    assert blocked_until == evening + 600
    assert_agree(state, permitted, status)

    # the admin's explicit "Free until" overrides everything, bedtime included
    with store as conn:
        budget.set_free_until(conn, bedtime.id, evening + 3600)
    state, permitted, status, _ = snapshot(store, bedtime.id, evening)
    assert status == "free"
    assert state["bedtime_active"] is True
    assert_agree(state, permitted, status)

    with store as conn:
        budget.update_group(conn, bedtime.id, paused=True)
    state, permitted, status, _ = snapshot(store, bedtime.id, evening)
    assert status == "paused"
    assert_agree(state, permitted, status)


def test_no_usage_is_charged_during_a_bedtime_window(store, fake):
    with store as conn:
        group = make_group(conn, windows=[a_window("21:00", "07:00")], minutes=60)
    reconciler = Reconciler(store, fake, TZ)
    reconciler.meter(ts_utc(2026, 11, 11, 21, 30), "startup")  # only sets the cursor

    fake.entries = [entry(ts_utc(2026, 11, 11, 21, 40))]
    assert reconciler.meter(ts_utc(2026, 11, 11, 21, 45)) == 0
    with store as conn:
        assert budget.metered_seconds(conn, group.id, "2026-11-11") == 0

    # the same activity after the window is charged as usual
    fake.entries = [entry(ts_utc(2026, 11, 12, 7, 5))]
    assert reconciler.meter(ts_utc(2026, 11, 12, 7, 10)) == 1
    with store as conn:
        assert budget.metered_seconds(conn, group.id, "2026-11-12") == BUCKET


# --------------------------------------------------------------------------- #
# the reconciler follows the window without any special case
# --------------------------------------------------------------------------- #


def test_adguard_blocks_during_bedtime_and_unblocks_after(store, fake):
    with store as conn:
        make_group(conn, windows=[a_window("21:00", "07:00")], domains=("example.com",))
    reconciler = Reconciler(store, fake, TZ)

    evening = ts_utc(2026, 11, 11, 22)
    reconciler.sync_services(evening)
    reconciler.sync_domains(evening)
    assert fake.ids == ["youtube"]
    assert "||example.com^" in fake.rules

    morning = ts_utc(2026, 11, 12, 8)
    reconciler.sync_services(morning)
    reconciler.sync_domains(morning)
    assert fake.ids == []
    assert fake.rules == []
