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
        "open_until",
        "status",
        "permitted",
        "blocked_until",
        "free_until",
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
