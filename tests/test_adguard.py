"""The AdGuard Home client: honest errors for odd responses."""

from __future__ import annotations

import pytest

from timefence.adguard import AdGuardClient, AdGuardError


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
