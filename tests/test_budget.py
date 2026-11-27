"""Group domain logic: validation, uniqueness, the status table and free time."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from timefence import budget, db

TZ = ZoneInfo("Asia/Kolkata")


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.db")
    db.migrate(connection)
    yield connection
    connection.close()


def make_group(
    conn,
    *,
    services=("youtube",),
    domains=(),
    windows=(),
    name="YouTube",
    paused=False,
):
    return budget.create_group(
        conn,
        name=name,
        emoji="▶️",
        services=list(services),
        domains=list(domains),
        windows=list(windows),
        paused=paused,
    )


def a_window(start, end, days="all"):
    return {"start": start, "end": end, "days": days}


def ts_on(year, month, day, hour, minute=0):
    return int(datetime(year, month, day, hour, minute, tzinfo=TZ).timestamp())


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://WWW.Example.com/path?x=1", "www.example.com"),
        ("http://example.com:8080/a/b", "example.com"),
        ("||foo.com^", "foo.com"),
        ("  Example.COM.  ", "example.com"),
        ("user:pass@sub.example.org/x", "sub.example.org"),
        ("//cdn.example.net/script.js", "cdn.example.net"),
        ("xn--bcher-kva.example", "xn--bcher-kva.example"),
    ],
)
def test_normalize_domain(raw, expected):
    assert budget.normalize_domain(raw) == expected


@pytest.mark.parametrize("raw", ["", "not a domain", "localhost", "http://", "-bad.com", "a..b"])
def test_normalize_domain_rejects(raw):
    with pytest.raises(budget.ValidationError):
        budget.normalize_domain(raw)


def test_normalize_domains_dedupes_and_keeps_order():
    text = "https://a.com/x\nb.com\nA.com\n# comment\n\nc.org\n"
    assert budget.normalize_domains(text) == ["a.com", "b.com", "c.org"]


def test_critical_match_covers_subdomains_and_prefix_wildcard():
    patterns = budget.load_critical_patterns()
    assert budget.critical_match("apps.apple.com", patterns) == "apps.apple.com"
    assert budget.critical_match("x.apps.apple.com", patterns) == "apps.apple.com"
    assert budget.critical_match("akamaihd.net", patterns) == "akamai*"
    assert budget.critical_match("example.com", patterns) is None


# --------------------------------------------------------------------------- #
# local days
# --------------------------------------------------------------------------- #


def test_parse_hhmm_rolls_to_tomorrow():
    now = ts_on(2026, 11, 11, 22)
    later = budget.parse_hhmm("23:30", now, TZ)
    assert datetime.fromtimestamp(later, TZ).day == 11
    tomorrow = budget.parse_hhmm("08:00", now, TZ)
    assert datetime.fromtimestamp(tomorrow, TZ).day == 12
    # "extra time until" a time that already passed today means tomorrow, and
    # the current minute is never "later" (the free window must last a while)
    assert datetime.fromtimestamp(budget.parse_hhmm("22:00", now, TZ), TZ).day == 12
    assert datetime.fromtimestamp(budget.parse_hhmm("21:59", now, TZ), TZ).day == 12
    assert datetime.fromtimestamp(budget.parse_hhmm("22:01", now, TZ), TZ).day == 11
    with pytest.raises(budget.ValidationError):
        budget.parse_hhmm("25:00", now, TZ)


NY = ZoneInfo("America/New_York")


def ts_on_ny(year, month, day, hour, minute=0):
    return int(datetime(year, month, day, hour, minute, tzinfo=NY).timestamp())


def test_next_local_midnight_on_dst_days():
    spring = ts_on_ny(2026, 3, 8, 12)
    autumn = ts_on_ny(2026, 11, 1, 12)

    spring_midnight = budget.next_local_midnight(spring, NY)
    autumn_midnight = budget.next_local_midnight(autumn, NY)

    assert datetime.fromtimestamp(spring_midnight, NY) == datetime(2026, 3, 9, 0, 0, tzinfo=NY)
    assert datetime.fromtimestamp(autumn_midnight, NY) == datetime(2026, 11, 2, 0, 0, tzinfo=NY)
    # a 23-hour and a 25-hour day: neither midnight is "now + 86400"
    assert spring_midnight - spring != 86400
    assert autumn_midnight - autumn != 86400


# --------------------------------------------------------------------------- #
# groups: creation, uniqueness and soft delete
# --------------------------------------------------------------------------- #


def test_a_group_needs_a_service_or_a_domain(conn):
    with pytest.raises(budget.ValidationError, match="at least one service or domain"):
        budget.create_group(conn, name="Nothing")
    with pytest.raises(budget.ValidationError):
        budget.create_group(conn, name="", services=["youtube"])
    with pytest.raises(budget.ValidationError, match="too long"):
        budget.create_group(conn, name="x" * 61, services=["youtube"])


def test_a_group_is_created_without_any_minutes(conn):
    group = make_group(conn)
    assert group.name == "YouTube"
    assert group.emoji == "▶️"
    assert group.services == ["youtube"]
    assert group.windows == []
    assert not hasattr(group, "weekday_minutes")
    assert not hasattr(group, "weekend_minutes")
    assert budget.is_permitted(conn, group, TZ, ts_on(2026, 11, 11, 3)) is True


def test_services_and_domains_are_unique_across_groups(conn):
    make_group(conn, services=("youtube",), domains=("example.com",))
    with pytest.raises(budget.ValidationError):
        make_group(conn, name="Other", services=("youtube",))
    with pytest.raises(budget.ValidationError):
        make_group(conn, name="Other2", services=(), domains=("example.com",))
    other = make_group(conn, name="Other3", services=(), domains=("other.com",))
    assert other.domains == ["other.com"]


def test_a_group_de_duplicates_its_services(conn):
    """Duplicate services in one payload must never reach the unique index."""
    group = make_group(conn, services=("YouTube", " youtube ", "youtube", "Roblox"))
    assert group.services == ["roblox", "youtube"]
    stored = conn.execute(
        "SELECT COUNT(*) FROM group_services WHERE group_id=?", (group.id,)
    ).fetchone()[0]
    assert stored == 2


def test_update_group_de_duplicates_its_services(conn):
    group = make_group(conn, services=("roblox",))
    updated = budget.update_group(conn, group.id, services=["Steam", "steam", " STEAM "])
    assert updated.services == ["steam"]


def test_group_name_uniqueness_matches_the_case_sensitive_index(conn):
    """Exact match after strip, like the index; the pre-check must agree with it."""
    make_group(conn, name="Games", services=("roblox",))
    with pytest.raises(budget.DuplicateName):
        make_group(conn, name="  Games  ", services=("steam",))
    lower = make_group(conn, name="games", services=("steam",))  # differs only by case
    assert lower.name == "games"
    # renaming a group to its own (padded) name is not a duplicate
    renamed = budget.update_group(conn, lower.id, name=" games ")
    assert renamed.name == "games"
    # a deleted group still frees its exact name
    budget.delete_group(conn, lower.id)
    reused = make_group(conn, name="games", services=("steam",))
    assert reused.name == "games"


def test_create_group_refuses_a_duplicate_name(conn):
    """The partial unique index must never be the thing that refuses it."""
    make_group(conn, name="Games", services=("roblox",))
    with pytest.raises(budget.ValidationError) as caught:
        make_group(conn, name="Games", services=("steam",))
    assert caught.value.code == "duplicate_name"
    assert conn.execute("SELECT COUNT(*) FROM groups WHERE deleted=0").fetchone()[0] == 1
    # a deleted group frees its name again, exactly like update_group
    old = budget.list_groups(conn)[0]
    budget.delete_group(conn, old.id)
    fresh = make_group(conn, name="Games", services=("steam",))
    assert fresh.name == "Games"


def test_a_window_error_names_its_reason(conn):
    with pytest.raises(budget.WindowError) as caught:
        make_group(conn, windows=[a_window("21:00", "21:00")])
    assert caught.value.reason == "start_equals_end"


def test_update_group_replaces_fields_and_keeps_the_rest(conn):
    group = make_group(conn, services=("roblox",), domains=("example.com",))
    updated = budget.update_group(conn, group.id, name="Renamed", emoji="🎮")
    assert (updated.name, updated.emoji) == ("Renamed", "🎮")
    assert updated.services == ["roblox"]
    assert updated.domains == ["example.com"]

    with pytest.raises(budget.ValidationError, match="at least one"):
        budget.update_group(conn, group.id, services=[], domains=[])
    kept = budget.get_group(conn, group.id)
    assert kept.services == ["roblox"] and kept.domains == ["example.com"]


def test_delete_group_frees_its_domains(conn):
    group = make_group(conn, services=(), domains=("example.com",))
    assert budget.delete_group(conn, group.id) == "YouTube"
    assert budget.list_groups(conn) == []
    assert len(budget.list_groups(conn, include_deleted=True)) == 1
    assert budget.get_group(conn, group.id).deleted is True
    # the domain can be reused now
    reused = make_group(conn, name="New", services=(), domains=("example.com",))
    assert reused.domains == ["example.com"]


def test_delete_clears_free_time(conn):
    group = make_group(conn)
    now = int(time.time())
    budget.set_free_until(conn, group.id, now + 3600)
    budget.delete_group(conn, group.id)
    assert budget.free_until(conn, group.id) is None


# --------------------------------------------------------------------------- #
# the one status predicate
# --------------------------------------------------------------------------- #

EVENING = ts_on(2026, 11, 11, 22)  # inside the "allowed" window below
OPENS = ts_on(2026, 11, 11, 21)  # the next opening for that window
CLOSES = ts_on(2026, 11, 11, 23)


def test_status_precedence_paused_free_blocked_outside_allowed(conn):
    group = make_group(conn, windows=[a_window("21:00", "23:00")])
    # 22:00 is inside the window -> allowed
    assert budget.group_status(conn, group, TZ, EVENING) == ("allowed", None)
    assert budget.is_permitted(conn, group, TZ, EVENING) is True

    # 12:00 is outside the window -> outside_hours, with the next opening
    noon = ts_on(2026, 11, 11, 12)
    assert budget.group_status(conn, group, TZ, noon) == ("outside_hours", OPENS)

    # an admin block wins over the window, inside and outside it
    budget.set_force_block(conn, group.id, EVENING + 600)
    group = budget.get_group(conn, group.id)
    assert budget.group_status(conn, group, TZ, EVENING) == ("blocked_admin", EVENING + 600)
    assert budget.is_permitted(conn, group, TZ, EVENING) is False

    # "Free until" wins over both the block and the window
    budget.set_free_until(conn, group.id, EVENING + 3600)
    assert budget.group_status(conn, group, TZ, EVENING) == ("free", None)
    assert budget.is_permitted(conn, group, TZ, EVENING) is True
    assert budget.group_status(conn, group, TZ, noon) == ("free", None)

    # pause wins over everything, including free time
    budget.update_group(conn, group.id, paused=True)
    group = budget.get_group(conn, group.id)
    assert budget.group_status(conn, group, TZ, EVENING) == ("paused", None)
    assert budget.is_permitted(conn, group, TZ, EVENING) is False


def test_free_time_expires_back_to_the_window(conn):
    group = make_group(conn, windows=[a_window("21:00", "23:00")])
    noon = ts_on(2026, 11, 11, 12)
    budget.set_free_until(conn, group.id, noon + 60)
    assert budget.group_status(conn, group, TZ, noon)[0] == "free"
    assert budget.group_status(conn, group, TZ, noon + 61) == ("outside_hours", OPENS)


def test_clear_free_and_clear_block(conn):
    group = make_group(conn, windows=[a_window("21:00", "23:00")])
    noon = ts_on(2026, 11, 11, 12)
    budget.set_free_until(conn, group.id, noon + 60)
    budget.clear_free(conn, group.id)
    assert budget.group_status(conn, group, TZ, noon)[0] == "outside_hours"
    budget.set_force_block(conn, group.id, noon + 60)
    group = budget.get_group(conn, group.id)
    assert budget.group_status(conn, group, TZ, noon)[0] == "blocked_admin"
    budget.clear_force_block(conn, group.id)
    group = budget.get_group(conn, group.id)
    assert budget.group_status(conn, group, TZ, noon)[0] == "outside_hours"


def test_is_permitted_always_agrees_with_the_status(conn):
    cases = [
        make_group(conn, name="All day", services=("youtube",)),
        make_group(conn, name="Window", services=("roblox",), windows=[a_window("21:00", "23:00")]),
    ]
    for group in cases:
        for moment in (EVENING, ts_on(2026, 11, 11, 12), ts_on(2026, 11, 12, 0)):
            status, _ = budget.group_status(conn, group, TZ, moment)
            assert budget.is_permitted(conn, group, TZ, moment) == (
                status in budget.PERMITTED_STATUSES
            )


def test_group_state_payload_has_the_documented_keys(conn):
    group = make_group(conn, windows=[a_window("21:00", "23:00")], domains=("example.com",))
    state = budget.group_state(conn, group, TZ, EVENING)
    assert set(state) == {
        "id",
        "name",
        "emoji",
        "windows",
        "open_now",
        "next_change",
        "opens_at",
        "opens_day",
        "open_until",
        "open_until_day",
        "status",
        "permitted",
        "blocked_until",
        "free_until",
        "free_until_day",
        "force_block_until",
        "paused",
        "owner_id",
        "services",
        "domains",
    }
    assert state["windows"] == [{"start": "21:00", "end": "23:00", "days": "all"}]
    assert state["permitted"] is True
    assert state["services"] == ["youtube"]
    assert state["domains"] == ["example.com"]

    free = budget.group_state(conn, group, TZ, EVENING)
    assert free["free_until"] is None
    budget.set_free_until(conn, group.id, EVENING + 60)
    state = budget.group_state(conn, group, TZ, EVENING)
    assert state["status"] == "free"
    assert state["free_until"] == EVENING + 60
    assert state["blocked_until"] is None


def test_no_usage_or_budget_helpers_remain():
    for name in (
        "add_bonus",
        "remaining_seconds",
        "budget_seconds",
        "cap_seconds",
        "used_seconds",
        "metered_seconds",
        "add_metered_seconds",
        "bonus_seconds",
        "reset_today",
        "history",
        "usage_range",
        "bedtime_state",
    ):
        assert not hasattr(budget, name), name


# --------------------------------------------------------------------------- #
# a future time names its day: "Opens tomorrow at 16:00", "Opens Sat at 16:00"
# --------------------------------------------------------------------------- #

SATURDAY_ONLY = [{"start": "16:00", "end": "18:00", "days": "sat"}]


def _state_at(conn, tz, year, month, day, hour, minute, windows, name="Group"):
    group = make_group(
        conn, windows=windows, name=name, services=(), domains=(f"{name.lower()}.test",)
    )
    now = int(datetime(year, month, day, hour, minute, tzinfo=tz).timestamp())
    return budget.group_state(conn, group, tz, now)


@pytest.mark.parametrize(
    ("day", "hour", "opens_at", "opens_day"),
    [
        # Friday 2026-11-13: the next opening is Saturday, in one local day
        (13, 9, "16:00", "tomorrow"),
        # Saturday before the window: it opens today, so no day is named
        (14, 9, "16:00", None),
        # Saturday after the window: the next one is a full week away
        (14, 19, "16:00", "sat"),
        # Sunday: six days out
        (15, 9, "16:00", "sat"),
        # Monday: five days out
        (16, 9, "16:00", "sat"),
    ],
)
def test_a_saturday_only_window_names_the_day_it_opens(conn, day, hour, opens_at, opens_day):
    state = _state_at(conn, TZ, 2026, 11, day, hour, 0, SATURDAY_ONLY)
    assert state["status"] == "outside_hours"
    assert state["opens_at"] == opens_at
    assert state["opens_day"] == opens_day


def test_an_overnight_window_keeps_a_bare_end_time(conn):
    """21:00-07:00: the end is on the next date and that reading is natural."""
    overnight = [{"start": "21:00", "end": "07:00", "days": "all"}]
    late = _state_at(conn, TZ, 2026, 11, 13, 23, 0, overnight, "Late")
    assert (late["status"], late["open_until"], late["open_until_day"]) == (
        "allowed",
        "07:00",
        None,
    )
    early = _state_at(conn, TZ, 2026, 11, 14, 3, 0, overnight, "Early")
    assert (early["status"], early["open_until"], early["open_until_day"]) == (
        "allowed",
        "07:00",
        None,
    )
    # outside the window at 12:00 the next opening is tonight, a bare time
    noon = _state_at(conn, TZ, 2026, 11, 14, 12, 0, overnight, "Noon")
    assert (noon["status"], noon["opens_at"], noon["opens_day"]) == ("outside_hours", "21:00", None)


def test_a_chained_open_span_a_whole_day_out_names_the_end_day(conn):
    """Chained windows can keep a group open for a whole day or more: reachable."""
    chained = [
        {"start": "08:00", "end": "20:00", "days": "mon"},
        {"start": "20:00", "end": "08:00", "days": "mon"},
        {"start": "08:00", "end": "20:00", "days": "tue"},
    ]
    state = _state_at(conn, TZ, 2026, 11, 16, 9, 0, chained)  # Monday 09:00
    assert state["status"] == "allowed"
    ahead = state["next_change"] - ts_on(2026, 11, 16, 9)
    assert ahead >= 86400, ahead  # the end really is a whole day out
    assert (state["open_until"], state["open_until_day"]) == ("20:00", "tomorrow")


def test_the_day_is_the_server_local_date_not_the_utc_one(tmp_path):
    """At 20:00 UTC it is already Saturday in Kiritimati (UTC+14)."""
    kiritimati = ZoneInfo("Pacific/Kiritimati")
    connection = db.connect(tmp_path / "k.db")
    try:
        db.migrate(connection)
        group = make_group(
            connection, windows=SATURDAY_ONLY, name="K", services=(), domains=("k.test",)
        )
        now = int(datetime(2026, 11, 13, 20, 0, tzinfo=ZoneInfo("UTC")).timestamp())
        state = budget.group_state(connection, group, kiritimati, now)
    finally:
        connection.close()
    assert datetime.fromtimestamp(now, kiritimati).strftime("%a %d") == "Sat 14"
    assert datetime.fromtimestamp(now, ZoneInfo("UTC")).strftime("%a %d") == "Fri 13"
    assert state["opens_at"] == "16:00"
    assert state["opens_day"] is None  # today in the server's zone


def test_the_day_label_survives_a_dst_change(conn):
    """London jumps on 2027-03-28; the dates, not the seconds, name the day."""
    london = ZoneInfo("Europe/London")
    week = _state_at(conn, london, 2027, 3, 27, 23, 30, SATURDAY_ONLY, "Week")
    assert (week["opens_at"], week["opens_day"]) == ("16:00", "sat")
    tomorrow = _state_at(
        conn,
        london,
        2027,
        3,
        27,
        23,
        30,
        [{"start": "09:00", "end": "10:00", "days": "sun"}],
        "Tomorrow",
    )
    assert (tomorrow["opens_at"], tomorrow["opens_day"]) == ("09:00", "tomorrow")


def test_only_outside_hours_names_its_day(conn):
    always = _state_at(conn, TZ, 2026, 11, 13, 9, 0, [], "Always")
    assert (always["opens_at"], always["opens_day"]) == (None, None)
    assert (always["open_until"], always["open_until_day"]) == (None, None)

    blocked = make_group(conn, windows=SATURDAY_ONLY, name="B", services=(), domains=("b.test",))
    budget.set_force_block(conn, blocked.id, ts_on(2026, 11, 13, 23))
    blocked = budget.get_group(conn, blocked.id)
    forced = budget.group_state(conn, blocked, TZ, ts_on(2026, 11, 13, 9))
    assert (forced["status"], forced["opens_day"], forced["open_until_day"]) == (
        "blocked_admin",
        None,
        None,
    )

    free = make_group(conn, windows=SATURDAY_ONLY, name="F", services=(), domains=("f.test",))
    budget.set_free_until(conn, free.id, ts_on(2026, 11, 13, 23))
    freed = budget.group_state(conn, free, TZ, ts_on(2026, 11, 13, 9))
    assert (freed["status"], freed["opens_day"], freed["open_until_day"]) == ("free", None, None)
    assert freed["free_until_day"] is None  # free until 23:00, today

    paused = make_group(
        conn, windows=SATURDAY_ONLY, paused=True, name="P", services=(), domains=("p.test",)
    )
    state = budget.group_state(conn, paused, TZ, ts_on(2026, 11, 13, 9))
    assert (state["status"], state["opens_day"]) == ("paused", None)


def test_free_time_names_the_day_it_ends(conn):
    """A free end is always under a day away, so the date alone names its day."""
    group = make_group(conn, windows=(), name="Free", services=(), domains=("free.test",))
    tonight = budget.parse_hhmm("02:00", ts_on(2026, 11, 13, 23), TZ)
    budget.set_free_until(conn, group.id, tonight)
    state = budget.group_state(conn, group, TZ, ts_on(2026, 11, 13, 23))
    # 23:00 -> 02:00 is tomorrow, and bare it would read as a time already past
    assert (state["status"], state["free_until_day"]) == ("free", "tomorrow")
    today = budget.parse_hhmm("21:00", ts_on(2026, 11, 13, 9), TZ)
    budget.set_free_until(conn, group.id, today)
    state = budget.group_state(conn, group, TZ, ts_on(2026, 11, 13, 9))
    assert (state["status"], state["free_until_day"]) == ("free", None)

    later = ts_on(2026, 11, 15, 9)
    budget.set_free_until(conn, group.id, later)
    state = budget.group_state(conn, group, TZ, ts_on(2026, 11, 13, 9))
    assert state["free_until_day"] == "sun"


def test_the_whole_day_gate_counts_wall_clock_time_on_an_autumn_day(conn):
    """London gains an hour on 2026-10-25: 23.5 wall hours read as 24.5 real ones."""
    london = ZoneInfo("Europe/London")
    # open from Saturday 10:00 to Sunday 09:30, chained across the 25-hour day
    windows = [
        {"start": "10:00", "end": "00:00", "days": "sat"},
        {"start": "00:00", "end": "09:30", "days": "sun"},
    ]
    state = _state_at(conn, london, 2026, 10, 24, 10, 0, windows, "Autumn")
    assert state["status"] == "allowed"
    end = datetime.fromtimestamp(state["next_change"], london)
    now = datetime.fromtimestamp(int(datetime(2026, 10, 24, 10, tzinfo=london).timestamp()), london)
    assert end.date() > now.date()
    # under a whole day by the wall clock, even though more than 24 h elapse
    assert (end.replace(tzinfo=None) - now.replace(tzinfo=None)).total_seconds() < 86400
    elapsed = state["next_change"] - int(datetime(2026, 10, 24, 10, tzinfo=london).timestamp())
    assert elapsed >= 86400
    assert state["open_until_day"] is None  # an overnight-style end, left bare


def test_the_whole_day_gate_counts_wall_clock_time_on_a_spring_day(conn):
    """London loses an hour on 2027-03-28: 24.5 wall hours read as 23.5 real ones."""
    london = ZoneInfo("Europe/London")
    windows = [
        {"start": "01:30", "end": "00:00", "days": "sat"},
        {"start": "00:00", "end": "02:00", "days": "sun"},
    ]
    state = _state_at(conn, london, 2027, 3, 27, 1, 30, windows, "Spring")
    assert state["status"] == "allowed"
    end = datetime.fromtimestamp(state["next_change"], london)
    now = datetime.fromtimestamp(
        int(datetime(2027, 3, 27, 1, 30, tzinfo=london).timestamp()), london
    )
    assert end.date() > now.date()
    # a whole day out by the wall clock, although fewer than 24 h elapse
    assert (end.replace(tzinfo=None) - now.replace(tzinfo=None)).total_seconds() >= 86400
    elapsed = state["next_change"] - int(datetime(2027, 3, 27, 1, 30, tzinfo=london).timestamp())
    assert elapsed < 86400
    assert state["open_until_day"] == "tomorrow"  # a full day ahead: named
