"""Budget maths, sessions, bonus/free time and input normalisation."""

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
    minutes=60,
    weekend=None,
    services=("youtube",),
    domains=(),
    name="YouTube",
    paused=False,
):
    return budget.create_group(
        conn,
        name=name,
        emoji="▶️",
        weekday_minutes=minutes,
        weekend_minutes=weekend,
        services=list(services),
        domains=list(domains),
        paused=paused,
    )


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
# weekday / weekend and midnight
# --------------------------------------------------------------------------- #


def test_weekday_and_weekend_minutes(conn):
    group = make_group(conn, minutes=60, weekend=120)
    wednesday = ts_on(2026, 11, 11, 12)  # Wednesday
    saturday = ts_on(2026, 11, 14, 12)
    assert group.minutes_for(wednesday, TZ) == 60
    assert group.minutes_for(saturday, TZ) == 120
    assert budget.budget_seconds(conn, group, TZ, wednesday) == 3600
    assert budget.budget_seconds(conn, group, TZ, saturday) == 7200


def test_day_boundary_and_local_midnight():
    late = ts_on(2026, 11, 13, 23, 59)
    early = ts_on(2026, 11, 14, 0, 1)
    assert budget.day_str(late, TZ) == "2026-11-13"
    assert budget.day_str(early, TZ) == "2026-11-14"
    assert budget.local_midnight(early, TZ) == ts_on(2026, 11, 14, 0)
    # a different day means a fresh budget
    assert budget.day_str(budget.local_midnight(early, TZ), TZ) == "2026-11-14"


def test_usage_does_not_leak_across_midnight(conn):
    group = make_group(conn, minutes=10)
    before_midnight = ts_on(2026, 11, 13, 23, 50)
    row = budget.start_session(conn, group, 5, TZ, actor="user", now=before_midnight)
    budget.close_session(conn, row["id"], "expired", before_midnight + 300)

    today = budget.day_str(before_midnight, TZ)
    tomorrow = budget.day_str(before_midnight + 86400, TZ)
    assert budget.used_seconds(conn, group.id, today, TZ, before_midnight + 400) == 300
    assert budget.used_seconds(conn, group.id, tomorrow, TZ, before_midnight + 86400) == 0
    assert budget.remaining_seconds(conn, group, TZ, before_midnight + 86400) == 600


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #


def test_session_is_capped_at_remaining_budget(conn):
    group = make_group(conn, minutes=2)
    now = ts_on(2026, 11, 11, 12)
    session = budget.start_session(conn, group, 60, TZ, actor="user", now=now)
    assert session["planned_seconds"] == 120
    assert budget.remaining_seconds(conn, group, TZ, now + 120) == 0


def test_stop_bills_only_elapsed_time(conn):
    group = make_group(conn, minutes=60)
    now = ts_on(2026, 11, 11, 12)
    session = budget.start_session(conn, group, 15, TZ, actor="user", now=now)
    assert session["planned_seconds"] == 900
    closed = budget.close_session(conn, session["id"], "stopped", now + 90)
    assert closed["used_seconds"] == 90
    assert budget.remaining_seconds(conn, group, TZ, now + 90) == 3600 - 90


def test_expiry_charges_the_planned_time(conn):
    group = make_group(conn, minutes=60)
    now = ts_on(2026, 11, 11, 12)
    session = budget.start_session(conn, group, 15, TZ, actor="user", now=now)
    closed = budget.close_expired(conn, now + 15 * 60)
    assert [c["id"] for c in closed] == [session["id"]]
    assert closed[0]["used_seconds"] == 900
    assert closed[0]["end_reason"] == "expired"


def test_no_budget_left_and_double_start(conn):
    group = make_group(conn, minutes=1)
    now = ts_on(2026, 11, 11, 12)
    budget.start_session(conn, group, 1, TZ, actor="user", now=now)
    with pytest.raises(budget.ValidationError):
        budget.start_session(conn, group, 1, TZ, actor="user", now=now + 5)
    budget.close_session(conn, budget.active_session(conn, group.id)["id"], "stopped", now + 60)
    with pytest.raises(budget.ValidationError):
        budget.start_session(conn, group, 1, TZ, actor="user", now=now + 120)


def test_paused_group_cannot_start(conn):
    group = make_group(conn, minutes=60, paused=True)
    with pytest.raises(budget.ValidationError):
        budget.start_session(conn, group, 15, TZ, actor="user", now=ts_on(2026, 11, 11, 12))


# --------------------------------------------------------------------------- #
# bonus and free-allow
# --------------------------------------------------------------------------- #


def test_bonus_minutes_extend_the_budget(conn):
    group = make_group(conn, minutes=10)
    now = ts_on(2026, 11, 11, 12)
    assert budget.remaining_seconds(conn, group, TZ, now) == 600
    budget.add_bonus(conn, group.id, 15, TZ, now)
    assert budget.remaining_seconds(conn, group, TZ, now) == 600 + 900
    # negative bonus is allowed (correction)
    budget.add_bonus(conn, group.id, -5, TZ, now)
    assert budget.remaining_seconds(conn, group, TZ, now) == 600 + 600


def test_free_allow_does_not_charge_budget(conn):
    group = make_group(conn, minutes=10)
    now = ts_on(2026, 11, 11, 12)
    budget.set_free_until(conn, group.id, now + 3600)
    assert budget.is_permitted(conn, group, TZ, now)
    assert not budget.is_permitted(conn, group, TZ, now + 3700)
    assert budget.remaining_seconds(conn, group, TZ, now) == 600
    assert budget.used_seconds(conn, group.id, budget.day_str(now, TZ), TZ, now) == 0


def test_parse_hhmm_rolls_to_tomorrow():
    now = ts_on(2026, 11, 11, 22)
    later = budget.parse_hhmm("23:30", now, TZ)
    assert datetime.fromtimestamp(later, TZ).day == 11
    tomorrow = budget.parse_hhmm("08:00", now, TZ)
    assert datetime.fromtimestamp(tomorrow, TZ).day == 12
    with pytest.raises(budget.ValidationError):
        budget.parse_hhmm("25:00", now, TZ)


# --------------------------------------------------------------------------- #
# groups: uniqueness, soft delete, history
# --------------------------------------------------------------------------- #


def test_services_and_domains_are_unique_across_groups(conn):
    make_group(conn, services=("youtube",), domains=("example.com",))
    with pytest.raises(budget.ValidationError):
        make_group(conn, name="Other", services=("youtube",))
    with pytest.raises(budget.ValidationError):
        make_group(conn, name="Other2", services=(), domains=("example.com",))
    other = make_group(conn, name="Other3", services=(), domains=("other.com",))
    assert other.domains == ["other.com"]


def test_delete_keeps_history_and_frees_the_domain(conn):
    group = make_group(conn, services=(), domains=("example.com",))
    now = ts_on(2026, 11, 11, 12)
    row = budget.start_session(conn, group, 15, TZ, actor="user", now=now)
    budget.close_session(conn, row["id"], "stopped", now + 60)

    budget.delete_group(conn, group.id)
    assert budget.list_groups(conn) == []
    assert len(budget.list_groups(conn, include_deleted=True)) == 1
    assert budget.history(conn, TZ, now=now)[-1][str(group.id)] == 1.0
    # the domain can be reused now
    reused = make_group(conn, name="New", services=(), domains=("example.com",))
    assert reused.domains == ["example.com"]


def test_group_state_reports_remaining_and_session(conn):
    group = make_group(conn, minutes=30)
    now = ts_on(2026, 11, 11, 12)
    budget.start_session(conn, group, 15, TZ, actor="user", now=now)
    state = budget.group_state(conn, group, TZ, now + 60)
    assert state["permitted"] is True
    assert state["active_session"]["ends_at"] == now + 900
    assert state["remaining_minutes"] == 30 - 1
    assert state["used_minutes"] == 1


def test_history_has_seven_days(conn):
    make_group(conn)
    now = int(time.time())
    rows = budget.history(conn, TZ, now=now)
    assert len(rows) == 7
    assert all("day" in row for row in rows)
