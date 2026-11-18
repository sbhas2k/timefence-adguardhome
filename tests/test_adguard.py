"""The AdGuard Home client: honest errors for odd responses."""

from __future__ import annotations

import pytest

from timefence.adguard import AdGuardClient, AdGuardError
from timefence.discover import parse_time


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
    assert client.calls == [None, newer]  # stopped: the second page reaches the window
    assert truncated is False


def test_querylog_since_reports_truncation():
    pages = [
        {"data": [{"time": "2026-11-12T16:00:00.000Z"}], "oldest": "2026-11-12T16:00:00.000Z"}
        for _ in range(3)
    ]
    client = PagingClient(pages)

    entries, truncated = client.querylog_since(0, max_pages=3)

    assert len(entries) == 3
    assert truncated is True
