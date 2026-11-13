"""Reconciler: section editing and read-modify-write of AdGuard settings."""

from __future__ import annotations

import time
from zoneinfo import ZoneInfo

import pytest

from timefence import budget, db
from timefence.reconciler import (
    SECTION_BEGIN,
    SECTION_END,
    Reconciler,
    build_rules,
    strip_section,
)

TZ = ZoneInfo("UTC")


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "r.db")
    db.migrate(connection)
    yield connection
    connection.close()


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


def test_service_sync_preserves_unmanaged_services_and_schedule(conn, fake):
    fake.ids = ["tiktok", "youtube"]
    fake.schedule = {"time_zone": "Asia/Kolkata", "mon": {"start": 0, "end": 86400}}
    add_group(conn, services=("youtube",))
    reconciler = Reconciler(conn, fake, TZ)

    changed = reconciler.sync_services(int(time.time()))
    assert "tiktok" in fake.ids  # foreign service kept
    assert "youtube" in fake.ids  # managed and blocked by default
    assert fake.schedule == {"time_zone": "Asia/Kolkata", "mon": {"start": 0, "end": 86400}}
    assert changed == []

    # already in sync -> no API write at all
    calls = len(fake.calls)
    reconciler.sync_services(int(time.time()))
    assert len(fake.calls) == calls


def test_service_sync_unblocks_a_permitted_group(conn, fake):
    group = add_group(conn, services=("youtube",))
    now = int(time.time())
    reconciler = Reconciler(conn, fake, TZ)
    reconciler.sync_services(now)
    assert fake.ids == ["youtube"]

    budget.start_session(conn, group, 15, TZ, actor="user", now=now)
    reconciler.sync_services(now + 10)
    assert fake.ids == []

    # free-allow time also unblocks without spending budget
    budget.stop_sessions(conn, group.id, "stopped", now + 20)
    budget.set_free_until(conn, group.id, now + 3600)
    reconciler.sync_services(now + 30)
    assert fake.ids == []


def test_paused_group_is_always_blocked(conn, fake):
    group = add_group(conn, services=("roblox",), minutes=60)
    now = int(time.time())
    reconciler = Reconciler(conn, fake, TZ)
    budget.update_group(conn, group.id, paused=True)
    reconciler.sync_services(now)
    assert fake.ids == ["roblox"]


def test_domain_sync_only_costs_one_write_per_change(conn, fake):
    group = add_group(conn, services=(), domains=("example.com",))
    reconciler = Reconciler(conn, fake, TZ)
    now = int(time.time())

    reconciler.sync_domains(now)
    assert fake.rules == [SECTION_BEGIN, "||example.com^", SECTION_END]

    calls = len(fake.calls)
    reconciler.sync_domains(now)
    assert len(fake.calls) == calls

    budget.start_session(conn, group, 15, TZ, actor="user", now=now)
    reconciler.sync_domains(now + 1)
    assert fake.rules == []


def test_reconciler_survives_adguard_errors(conn, fake, monkeypatch):
    def boom(*args, **kwargs):
        from timefence.adguard import AdGuardError

        raise AdGuardError("down")

    monkeypatch.setattr(fake, "blocked_services", boom)
    add_group(conn)
    result = Reconciler(conn, fake, TZ).run("test")
    assert result["errors"]
