"""Reconciler: section editing and read-modify-write of AdGuard settings."""

from __future__ import annotations

import time
from datetime import UTC
from zoneinfo import ZoneInfo

import pytest

from timefence import budget, db
from timefence.reconciler import (
    BUCKET,
    SECTION_BEGIN,
    SECTION_END,
    Reconciler,
    build_rules,
    rule_host,
    strip_section,
)

TZ = ZoneInfo("UTC")


@pytest.fixture()
def store(tmp_path):
    store = db.Store(tmp_path / "r.db")
    with store as conn:
        db.migrate(conn, TZ)
    yield store
    store.close()


def add_group(conn, name="YouTube", services=("youtube",), domains=(), minutes=60):
    return budget.create_group(
        conn,
        name=name,
        emoji="",
        weekday_minutes=minutes,
        weekend_minutes=minutes,
        services=list(services),
        domains=list(domains),
    )


# --------------------------------------------------------------------------- #
# rule section editing
# --------------------------------------------------------------------------- #


def test_build_rules_preserves_foreign_rules():
    current = [
        "||ads.example^",
        SECTION_BEGIN,
        "||old.com^",
        SECTION_END,
        "!#include something",
        "@@||allowed.example^",
    ]
    result = build_rules(current, ["new.com", "other.org"])
    assert result == [
        "||ads.example^",
        "!#include something",
        "@@||allowed.example^",
        SECTION_BEGIN,
        "||new.com^",
        "||other.org^",
        SECTION_END,
    ]


def test_build_rules_without_domains_removes_the_section_only():
    current = ["||keep.example^", SECTION_BEGIN, "||old.com^", SECTION_END]
    assert build_rules(current, []) == ["||keep.example^"]


def test_build_rules_migrates_legacy_budget_app_section():
    current = [
        "||keep.example^",
        "# budget-app:begin",
        "||legacy.com^",
        "# budget-app:end",
    ]
    assert build_rules(current, ["new.com"]) == [
        "||keep.example^",
        SECTION_BEGIN,
        "||new.com^",
        SECTION_END,
    ]


def test_build_rules_is_idempotent():
    once = build_rules(["||keep^"], ["a.com"])
    assert build_rules(once, ["a.com"]) == once
    assert strip_section(once) == ["||keep^"]


# --------------------------------------------------------------------------- #
# AdGuard read-modify-write
# --------------------------------------------------------------------------- #


def test_service_sync_preserves_unmanaged_services_and_schedule(store, fake):
    fake.ids = ["tiktok", "youtube"]
    fake.schedule = {"time_zone": "Asia/Kolkata", "mon": {"start": 0, "end": 86400}}
    with store as conn:
        group = add_group(conn, services=("youtube",))
    reconciler = Reconciler(store, fake, TZ)

    changed = reconciler.sync_services(int(time.time()))
    assert fake.ids == ["tiktok"]  # allowed until the daily minutes are used
    assert changed == ["youtube"]

    # a paused group is blocked; foreign services and the schedule survive
    with store as conn:
        budget.update_group(conn, group.id, paused=True)
    reconciler.sync_services(int(time.time()))
    assert "tiktok" in fake.ids
    assert "youtube" in fake.ids
    assert fake.schedule == {"time_zone": "Asia/Kolkata", "mon": {"start": 0, "end": 86400}}

    # already in sync -> no API write at all
    calls = len(fake.calls)
    reconciler.sync_services(int(time.time()))
    assert len(fake.calls) == calls


def test_service_sync_unblocks_a_permitted_group(store, fake):
    with store as conn:
        group = add_group(conn, services=("youtube",))
    now = int(time.time())
    reconciler = Reconciler(store, fake, TZ)
    reconciler.sync_services(now)
    assert fake.ids == []

    with store as conn:
        budget.set_force_block(conn, group.id, now + 3600)
    reconciler.sync_services(now + 10)
    assert fake.ids == ["youtube"]

    # free-allow time overrides the block without spending budget
    with store as conn:
        budget.set_free_until(conn, group.id, now + 3600)
    reconciler.sync_services(now + 30)
    assert fake.ids == []


def test_paused_group_is_always_blocked(store, fake):
    with store as conn:
        group = add_group(conn, services=("roblox",), minutes=60)
        budget.update_group(conn, group.id, paused=True)
    reconciler = Reconciler(store, fake, TZ)
    reconciler.sync_services(int(time.time()))
    assert fake.ids == ["roblox"]


def test_domain_sync_only_costs_one_write_per_change(store, fake):
    with store as conn:
        group = add_group(conn, services=(), domains=("example.com",))
    reconciler = Reconciler(store, fake, TZ)
    now = int(time.time())

    reconciler.sync_domains(now)
    assert fake.rules == []

    calls = len(fake.calls)
    reconciler.sync_domains(now)
    assert len(fake.calls) == calls

    with store as conn:
        budget.set_force_block(conn, group.id, now + 3600)
    reconciler.sync_domains(now + 1)
    assert fake.rules == [SECTION_BEGIN, "||example.com^", SECTION_END]

    with store as conn:
        budget.clear_force_block(conn, group.id)
    reconciler.sync_domains(now + 2)
    assert fake.rules == []


def test_reconciler_survives_adguard_errors(store, fake, monkeypatch):
    def boom(*args, **kwargs):
        from timefence.adguard import AdGuardError

        raise AdGuardError("down")

    monkeypatch.setattr(fake, "blocked_services", boom)
    with store as conn:
        add_group(conn)
    result = Reconciler(store, fake, TZ).run("test")
    assert result["errors"]


# --------------------------------------------------------------------------- #
# auto mode ("allowed until the budget runs out") + metering
# --------------------------------------------------------------------------- #


def test_auto_mode_is_open_until_the_budget_is_spent(store, fake):
    with store as conn:
        group = add_group(conn, services=("youtube",), minutes=1)
    reconciler = Reconciler(store, fake, TZ)
    now = int(time.time())

    reconciler.sync_services(now)
    assert fake.ids == []  # nothing blocked by default

    # pretend a minute of usage was metered
    with store as conn:
        budget.add_metered_seconds(conn, group.id, budget.day_str(now, TZ), 60, cap_seconds=60)
        group = budget.get_group(conn, group.id)
        assert budget.remaining_seconds(conn, group, TZ, now) == 0
    reconciler.sync_services(now)
    assert fake.ids == ["youtube"]  # limit reached -> blocked


def test_metering_charges_time_only_when_the_group_was_queried(store, fake):
    with store as conn:
        youtube = add_group(conn, services=("youtube",), minutes=30)
        roblox = add_group(conn, name="Roblox", services=("roblox",), minutes=30)
    reconciler = Reconciler(store, fake, TZ)
    now = int(time.time())

    # first pass only sets the cursor
    reconciler.meter(now, "startup")
    with store as conn:
        assert budget.metered_seconds(conn, youtube.id, budget.day_str(now, TZ)) == 0

    fake.entries = [
        {
            "time": "2026-11-13T07:00:00.000000000Z",
            "client": "192.168.1.50",
            "question": {"name": "rr1---sn-abc.googlevideo.com", "type": "A"},
        }
    ]
    # entry timestamp is outside the window on purpose -> nothing charged
    reconciler.meter(now + 30)
    with store as conn:
        assert budget.metered_seconds(conn, youtube.id, budget.day_str(now, TZ)) == 0

    # now a fresh entry inside the window
    from datetime import datetime

    stamp = datetime.fromtimestamp(now + 40, UTC).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    fake.entries = [
        {
            "time": stamp,
            "client": "192.168.1.50",
            "question": {"name": "rr1---sn-abc.googlevideo.com", "type": "A"},
        },
        {
            "time": stamp,
            "client": "127.0.0.1",  # our own lookups never count
            "question": {"name": "roblox.com", "type": "A"},
        },
    ]
    charged = reconciler.meter(now + 60)
    assert charged == 1
    day = budget.day_str(now + 60, TZ)
    with store as conn:
        assert budget.metered_seconds(conn, youtube.id, day) == BUCKET
        assert budget.metered_seconds(conn, roblox.id, day) == 0


def test_auto_mode_blocks_for_the_rest_of_the_day_then_allows_tomorrow(store, fake):
    now = int(time.time())
    day = budget.day_str(now, TZ)
    with store as conn:
        group = add_group(conn, services=("youtube",), minutes=1)
        budget.add_metered_seconds(conn, group.id, day, 60, cap_seconds=60)
    reconciler = Reconciler(store, fake, TZ)

    reconciler.sync_services(now)
    assert fake.ids == ["youtube"]
    tomorrow = now + 86400
    with store as conn:
        assert budget.is_permitted(conn, group, TZ, tomorrow) is True
    reconciler.sync_services(tomorrow)
    assert fake.ids == []


# --------------------------------------------------------------------------- #
# resilience, damaged rule sections and release
# --------------------------------------------------------------------------- #


def test_unexpected_exception_does_not_abort_the_run(store, fake, monkeypatch):
    now = int(time.time())
    with store as conn:
        group = add_group(conn, services=("youtube",), domains=("example.com",))
        budget.set_force_block(conn, group.id, now + 3600)

    def boom(*args, **kwargs):
        raise ValueError("broken")

    monkeypatch.setattr(fake, "blocked_services", boom)
    result = Reconciler(store, fake, TZ).run("test")

    assert "internal error: ValueError" in result["errors"]
    assert "||example.com^" in fake.rules  # the domain phase still ran


def test_unbalanced_begin_keeps_foreign_rules():
    result = build_rules(["||f1^", SECTION_BEGIN, "||a.com^", "||f2^"], ["b.com"])
    assert result == ["||f1^", "||a.com^", "||f2^", SECTION_BEGIN, "||b.com^", SECTION_END]


def test_stray_end_is_dropped_alone():
    assert build_rules(["||f1^", SECTION_END, "||f2^"], []) == ["||f1^", "||f2^"]


def test_release_removes_only_owned_state(store, fake):
    with store as conn:
        add_group(conn, services=("youtube",), domains=("example.com",))
    fake.ids = ["youtube", "tiktok"]
    fake.rules = ["||foreign.example^", SECTION_BEGIN, "||example.com^", SECTION_END]

    removed = Reconciler(store, fake, TZ).release()

    assert "youtube" in removed["services"]
    assert fake.ids == ["tiktok"]  # a foreign service survives
    assert fake.rules == ["||foreign.example^"]  # so does a foreign rule


def test_a_null_user_rules_answer_is_an_empty_list(store):
    # the real server sends null, not [], when no rule exists
    from .fakes import FaithfulAdGuard

    fake = FaithfulAdGuard()
    reconciler = Reconciler(store, fake, TZ)

    result = reconciler.run("loop", now=int(time.time()))
    assert result["errors"] == []
    assert fake.user_rules() is None
    assert reconciler.release() == {"services": [], "rules_removed": 0}
    assert reconciler.last_error is None


def test_service_rule_hosts_from_the_real_catalogue(store):
    from .fakes import FaithfulAdGuard

    reconciler = Reconciler(store, FaithfulAdGuard(), TZ)
    domains = reconciler.service_domains()

    assert "youtube.com" in domains["youtube"]
    assert "spotify.com" in domains["spotify"]
    assert "audio-ak-spotify-com.akamaized.net" in domains["spotify"]
    assert "spotify.com.edgesuite.net" in domains["spotify"]
    assert "qq.com" in domains["qq"]
    assert "mask.icloud.com" in domains["icloud_private_relay"]
    assert "amazonaws.com" in domains["amazon"]
    for hosts in domains.values():
        assert not [host for host in hosts if "^" in host or "$" in host or "|" in host]
    assert "vod-adaptive.akamaized.net" not in domains["vimeo"]  # a wildcard names no host
    assert "ebay-*.s3-us-west-1.amazonaws.com" not in domains["ebay"]


@pytest.mark.parametrize(
    ("rule", "host"),
    [
        ("||youtube.com^", "youtube.com"),
        ("|audio-ak-spotify-com.akamaized.net^", "audio-ak-spotify-com.akamaized.net"),
        ("||qq.com^$denyallow=wx.qq.com|weixin.qq.com", "qq.com"),
        ("||amazonaws.com^$dnstype=~CNAME", "amazonaws.com"),
        ("||mask.icloud.com^$dnsrewrite=NXDOMAIN;;", "mask.icloud.com"),
        ("||Example.COM^", "example.com"),
        ("example.com", "example.com"),
        ("/_spotify-connect._tcp.local/", None),
        ("*vod-adaptive.akamaized.net^", None),
        ("|ebay-*.s3-us-west-1.amazonaws.com^", None),
        ("||awsdns-*.com^", None),
        ("! a comment", None),
        ("@@||allowed.example^", None),
        ("", None),
        ("   ", None),
    ],
)
def test_rule_host_parses_the_real_rule_shapes(rule, host):
    assert rule_host(rule) == host
