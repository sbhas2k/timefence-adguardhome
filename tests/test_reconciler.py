"""Reconciler: section editing and read-modify-write of AdGuard settings."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from timefence import budget, db
from timefence.reconciler import (
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


def add_group(conn, name="YouTube", services=("youtube",), domains=(), windows=()):
    return budget.create_group(
        conn,
        name=name,
        emoji="",
        services=list(services),
        domains=list(domains),
        windows=list(windows),
    )


def ts_utc(year, month, day, hour, minute=0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp())


def a_window(start, end, days="all"):
    return {"start": start, "end": end, "days": days}


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
    assert fake.ids == ["tiktok"]  # the group has no windows, so it is allowed
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
        group = add_group(conn, services=("roblox",))
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
# allowed windows drive the block, with no usage metering at all
# --------------------------------------------------------------------------- #


def test_outside_the_window_the_group_is_blocked_and_inside_it_is_not(store, fake):
    with store as conn:
        add_group(
            conn,
            services=("youtube",),
            domains=("example.com",),
            windows=[a_window("13:00", "14:00")],
        )
    reconciler = Reconciler(store, fake, TZ)

    outside = ts_utc(2026, 11, 11, 12, 59)
    reconciler.sync_services(outside)
    reconciler.sync_domains(outside)
    assert fake.ids == ["youtube"]
    assert fake.rules == [SECTION_BEGIN, "||example.com^", SECTION_END]

    # the window opens: one reconcile pass lifts the block at the edge
    inside = ts_utc(2026, 11, 11, 13)
    reconciler.sync_services(inside)
    reconciler.sync_domains(inside)
    assert fake.ids == []
    assert fake.rules == []

    # a second pass with the same state writes nothing (idempotent)
    calls = len(fake.calls)
    reconciler.sync_services(inside + 30)
    reconciler.sync_domains(inside + 30)
    assert len(fake.calls) == calls

    # and the closing edge blocks again within one pass
    closing = ts_utc(2026, 11, 11, 14)
    reconciler.sync_services(closing)
    reconciler.sync_domains(closing)
    assert fake.ids == ["youtube"]
    assert "||example.com^" in fake.rules


def test_a_wrapping_window_blocks_across_midnight(store, fake):
    with store as conn:
        add_group(conn, services=("roblox",), windows=[a_window("22:00", "07:00")])
    reconciler = Reconciler(store, fake, TZ)

    reconciler.sync_services(ts_utc(2026, 11, 11, 23))
    assert fake.ids == []
    reconciler.sync_services(ts_utc(2026, 11, 12, 7))
    assert fake.ids == ["roblox"]
    reconciler.sync_services(ts_utc(2026, 11, 12, 21, 59))
    assert fake.ids == ["roblox"]
    reconciler.sync_services(ts_utc(2026, 11, 12, 22))
    assert fake.ids == []


def test_free_time_unblocks_a_group_outside_its_window(store, fake):
    with store as conn:
        group = add_group(conn, services=("youtube",), windows=[a_window("13:00", "14:00")])
    reconciler = Reconciler(store, fake, TZ)
    noon = ts_utc(2026, 11, 11, 12)
    reconciler.sync_services(noon)
    assert fake.ids == ["youtube"]

    with store as conn:
        budget.set_free_until(conn, group.id, noon + 600)
    reconciler.sync_services(noon + 60)
    assert fake.ids == []

    # the free window ends, so the schedule blocks again
    reconciler.sync_services(noon + 601)
    assert fake.ids == ["youtube"]


def test_an_admin_block_wins_over_an_open_window(store, fake):
    with store as conn:
        group = add_group(conn, services=("youtube",), windows=[a_window("13:00", "14:00")])
    reconciler = Reconciler(store, fake, TZ)
    inside = ts_utc(2026, 11, 11, 13, 30)
    with store as conn:
        budget.set_force_block(conn, group.id, inside + 600)
    reconciler.sync_services(inside)
    assert fake.ids == ["youtube"]


def test_the_reconciler_never_reads_the_query_log(store, fake):
    """Metering is gone: only the discovery screen may ask for the query log."""
    now = ts_utc(2026, 11, 11, 12)
    with store as conn:
        group = add_group(conn, services=("youtube",))
        budget.set_force_block(conn, group.id, now + 3600)
    reconciler = Reconciler(store, fake, TZ)

    reconciler.run("loop", now=now)

    # it synced (a services write) without ever asking for the query log
    assert [entry for entry in fake.calls if entry[0] == "services"]
    assert [entry for entry in fake.calls if entry[0].startswith("querylog")] == []


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


def test_rule_host_parses_the_real_catalogue():
    from .fakes import load_service_catalogue

    hosts = {
        service["id"]: {host for host in map(rule_host, service.get("rules") or []) if host}
        for service in load_service_catalogue()
    }

    assert "youtube.com" in hosts["youtube"]
    assert "spotify.com" in hosts["spotify"]
    assert "audio-ak-spotify-com.akamaized.net" in hosts["spotify"]
    assert "spotify.com.edgesuite.net" in hosts["spotify"]
    assert "qq.com" in hosts["qq"]
    assert "mask.icloud.com" in hosts["icloud_private_relay"]
    assert "amazonaws.com" in hosts["amazon"]
    for service_hosts in hosts.values():
        assert not [host for host in service_hosts if "^" in host or "$" in host or "|" in host]
    assert "vod-adaptive.akamaized.net" not in hosts["vimeo"]  # a wildcard names no host
    assert "ebay-*.s3-us-west-1.amazonaws.com" not in hosts["ebay"]


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
