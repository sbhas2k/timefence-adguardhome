"""The AdGuard Home client: honest errors for odd responses."""

from __future__ import annotations

import pytest
import requests

from timefence.adguard import (
    AdGuardAuthError,
    AdGuardClient,
    AdGuardError,
    next_timestamp,
)
from timefence.discover import parse_time

from .fakes import FaithfulAdGuard, StubServer, real_entry


class StubResponse:
    status_code = 200
    content = b"<html>not json</html>"
    text = "<html>not json</html>"

    def json(self):
        raise ValueError("not JSON")


class StubSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.requests: list[tuple[str, str]] = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url))
        return StubResponse()


def test_non_json_response_raises_adguard_error():
    client = AdGuardClient("http://adguard.test", "admin", "secret", session=StubSession())
    with pytest.raises(AdGuardError, match="unexpected response"):
        client.blocked_services()
    with pytest.raises(AdGuardError, match="unexpected response"):
        client.user_rules()
    with pytest.raises(AdGuardError, match="unexpected response"):
        client.all_services()


def test_a_rejected_credential_is_its_own_error():
    # the real server answers 401 with an empty body, for a wrong password and
    # also while its brute-force lock is active
    for status in (401, 403):
        with StubServer(status=status, body=b"") as stub:
            client = AdGuardClient(stub.url, "admin", "wrong")
            with pytest.raises(AdGuardAuthError) as caught:
                client.blocked_services()
        assert isinstance(caught.value, AdGuardError)
        assert "credentials" in str(caught.value)


class PagingClient(AdGuardClient):
    """Serves canned query-log pages and records the `older_than` of each call."""

    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.calls: list[str | None] = []

    def querylog_page(self, limit: int = 500, older_than: str | None = None) -> dict:
        self.calls.append(older_than)
        return self.pages[len(self.calls) - 1]


def test_querylog_since_stops_at_window_start():
    older = "2026-11-12T15:59:00.000Z"
    newer = "2026-11-12T16:00:00.000Z"
    client = PagingClient(
        [
            {"data": [{"time": newer}], "oldest": newer},
            {"data": [{"time": older}], "oldest": older},
            {"data": [], "oldest": ""},
        ]
    )

    entries, truncated = client.querylog_since(parse_time("2026-11-12T15:59:30Z"))

    assert [entry["time"] for entry in entries] == [newer, older]
    # stopped: the second page reaches the window past its first entry
    assert client.calls == [None, next_timestamp(newer)]
    assert truncated is False


def test_querylog_since_reports_truncation():
    pages = [
        {
            "data": [{"time": f"2026-11-12T16:0{index}:00.000Z"}],
            "oldest": f"2026-11-12T16:0{index}:00.000Z",
        }
        for index in (3, 2, 1)
    ]
    client = PagingClient(pages)

    entries, truncated = client.querylog_since(0, max_pages=3)

    assert len(entries) == 3
    assert truncated is True


def tied_entries(total: int = 2850) -> list[dict]:
    """Realistic ties: 6 to 10 entries share a stamp, unaligned with any page."""
    entries = []
    group = index = 0
    size = 6
    for _ in range(total):
        stamp = f"2026-11-19T12:{group // 60:02d}:{group % 60:02d}.0000000Z"
        entries.append(real_entry(f"q{group}-{index}.example.org", stamp))
        index += 1
        if index >= size:
            group += 1
            index = 0
            size = 6 + (group % 5)
    return entries


def test_querylog_since_reads_every_entry_when_timestamps_tie():
    fake = FaithfulAdGuard()
    fake.entries = tied_entries()
    since = parse_time("2026-11-19T12:00:00.0000000Z")

    entries, truncated = fake.querylog_since(since)

    keys = {(entry["time"], entry["question"]["name"]) for entry in entries}
    assert len(keys) == 2850
    assert truncated is False
    assert fake.querylog_page(limit=5000)["data"]  # a larger page is honoured


def test_a_timestamp_is_bumped_by_its_own_precision():
    assert next_timestamp("2026-11-19T12:28:17.2765874+05:30") == (
        "2026-11-19T12:28:17.2765884+05:30"
    )
    assert next_timestamp("2026-11-19T12:28:17.276Z") == "2026-11-19T12:28:17.277Z"
    assert next_timestamp("2026-11-19T12:28:17.889389948Z") == "2026-11-19T12:28:17.889390948Z"
    assert next_timestamp("2026-11-19T12:28:17.9999999Z") == "2026-11-19T12:28:18.0000000Z"
    assert next_timestamp("2026-11-19T12:28:17Z") == "2026-11-19T12:28:18Z"
    assert next_timestamp("not a time") is None


class RefusingSession:
    """The raw text a refused connection produces, without a real socket."""

    headers: dict[str, str] = {}

    def request(self, *args, **kwargs):
        raise requests.exceptions.ConnectionError(
            "HTTPConnectionPool(host='127.0.0.1', port=3100): Max retries exceeded with url: "
            "/control/blocked_services/get (Caused by NewConnectionError(...))"
        )


def test_a_transport_failure_is_a_short_category():
    client = AdGuardClient("http://127.0.0.1:3100", "admin", "secret", session=RefusingSession())
    with pytest.raises(AdGuardError) as caught:
        client.blocked_services()
    text = str(caught.value)
    assert text == "cannot reach AdGuard Home"
    for leak in ("HTTPConnectionPool", "Max retries", "3100", "http://", "GET ", "secret"):
        assert leak not in text


def test_a_silent_server_is_reported_as_a_timeout():
    with StubServer(delay=0.5) as stub:
        client = AdGuardClient(stub.url, "admin", "secret", timeout=0.2)
        with pytest.raises(AdGuardError) as caught:
            client.blocked_services()
    text = str(caught.value)
    assert text == "AdGuard Home did not answer within 0.2 s"
    assert stub.url not in text


def test_querylog_since_keeps_ties_across_small_pages():
    fake = FaithfulAdGuard()
    fake.entries = tied_entries()
    since = parse_time("2026-11-19T12:00:00.0000000Z")

    entries, truncated = fake.querylog_since(since, page_size=500)

    keys = {(entry["time"], entry["question"]["name"]) for entry in entries}
    assert len(keys) == 2850
    assert truncated is False
