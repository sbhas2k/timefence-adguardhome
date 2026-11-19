"""The second stand-in mirrors the real AdGuard Home, quirks included.

These proofs test the double itself: if it drifts back towards the old, too
tidy fake, the defects it exists to reproduce can no longer be seen.
"""

from __future__ import annotations

import pytest

from timefence.adguard import AdGuardAuthError, AdGuardClient

from .fakes import FaithfulAdGuard, load_service_catalogue, real_entry


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_five_failures_lock_the_address_and_a_correct_password_no_longer_helps() -> None:
    clock = Clock()
    fake = FaithfulAdGuard(password="wrong", now=clock)
    for _ in range(5):
        with pytest.raises(AdGuardAuthError):
            fake.blocked_services()
    assert fake.blocked_until == 15 * 60

    fake.password = "secret"
    with pytest.raises(AdGuardAuthError):
        fake.blocked_services()

    clock.now = 15 * 60
    assert fake.blocked_services()["ids"] == []
    assert fake.auth_failures == []


def test_a_backed_off_retry_never_reaches_the_lock() -> None:
    # 30 s, then doubling to 15 min: old failures fall out of the window before
    # a fifth can join them, which is the point of backing off at all
    clock = Clock()
    fake = FaithfulAdGuard(password="wrong", now=clock)
    delay = 30
    for _ in range(10):
        clock.now += delay
        delay = min(delay * 2, 900)
        with pytest.raises(AdGuardAuthError):
            fake.blocked_services()
    assert fake.blocked_until == 0.0


def test_user_rules_are_null_when_empty_like_the_real_server() -> None:
    fake = FaithfulAdGuard()
    assert fake.user_rules() is None
    fake.rules = ["||example.com^"]
    assert fake.user_rules() == ["||example.com^"]


def test_a_strict_cursor_loses_entries_that_share_the_page_boundary() -> None:
    fake = FaithfulAdGuard()
    stamp = "2026-11-19T12:28:17.2765874+05:30"
    earlier = "2026-11-19T12:28:00.0000000+05:30"
    fake.entries = [real_entry(f"q{index}.example.org", stamp) for index in range(6)]
    fake.entries.append(real_entry("late.example.org", earlier))

    first = fake.querylog_page(limit=3)
    assert len(first["data"]) == 3
    second = fake.querylog_page(limit=3, older_than=first["oldest"])
    # the three other rows with the same timestamp are older-but-not-strictly
    assert {entry["question"]["name"] for entry in second["data"]} == {"late.example.org"}


def test_a_large_page_is_not_capped() -> None:
    fake = FaithfulAdGuard()
    fake.entries = [
        real_entry(f"q{index}.example.org", f"2026-11-19T12:00:{index % 60:02d}.0000000Z")
        for index in range(1200)
    ]
    assert len(fake.querylog_page(limit=5000)["data"]) == 1200


def test_the_catalogue_keeps_the_real_rule_shapes_and_icon_sizes() -> None:
    catalogue = load_service_catalogue()
    assert 12 <= len(catalogue) <= 15
    rules = {service["id"]: service["rules"] for service in catalogue}
    assert "||youtube.com^" in rules["youtube"]
    assert rules["youtube"].index("||ggpht.cn^") < rules["youtube"].index("||youtube.com^")
    assert any(rule.startswith("|") and not rule.startswith("||") for rule in rules["spotify"])
    assert "/_spotify-connect._tcp.local/" in rules["spotify"]
    assert any("$denyallow=" in rule for rule in rules["qq"])
    assert any(rule.startswith("*") for rule in rules["vimeo"])
    icons = {service["id"]: len(service["icon_svg"]) for service in catalogue}
    assert icons["peacock_tv"] > 8192


def test_service_blocks_and_user_rules_answer_with_different_reasons() -> None:
    fake = FaithfulAdGuard()
    fake.ids = ["youtube"]
    fake.rules = ["||blocked.example^"]
    assert fake.check_host("www.youtube.com")["reason"] == "FilteredBlockedService"
    assert fake.check_host("www.blocked.example")["reason"] == "FilteredBlackList"
    assert fake.check_host("allowed.example")["reason"] == "NotFilteredNotFound"


def test_querylog_config_exposes_ignored_enabled() -> None:
    config = FaithfulAdGuard().querylog_config()
    assert config is not None
    assert config["ignored_enabled"] is False


def test_the_double_is_still_a_client_for_paging_proofs() -> None:
    assert isinstance(FaithfulAdGuard(), AdGuardClient)
