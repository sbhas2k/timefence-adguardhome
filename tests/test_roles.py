"""Role boundaries: a user account is read-only and sees a reduced payload."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from timefence import i18n
from timefence.api import create_app

ADMIN = {"username": "admin", "password": "admin"}
ADMIN2 = {"username": "admin", "password": "correct horse battery"}

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
EXEMPT = {"/api/login", "/api/logout"}

APP_KEYS = {
    "name",
    "version",
    "role",
    "now",
    "timezone",
    "username",
    "csrf",
    "setup_complete",
}
GROUP_KEYS = {
    "id",
    "name",
    "emoji",
    "permitted",
    "status",
    "open_now",
    "opens_at",
    "open_until",
    "free_until",
    "paused",
}


def sign_in(client, username: str, password: str) -> str:
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    csrf = response.json()["csrf"]
    client.headers.update({"X-CSRF-Token": csrf})
    return csrf


def complete_setup(client) -> None:
    sign_in(client, "admin", "admin")
    assert client.post("/api/setup", json={"admin_password": ADMIN2["password"]}).status_code == 200
    sign_in(client, "admin", ADMIN2["password"])


def create_kid(client, username: str = "kid", password: str = "kid-password") -> dict:
    created = client.post(
        "/api/users", json={"username": username, "password": password, "role": "user"}
    )
    assert created.status_code == 200, created.text
    return created.json()["user"]


def test_every_mutating_route_is_forbidden_for_users(settings, fake):
    client = TestClient(create_app(settings, adguard=fake))
    with client:
        complete_setup(client)
        create_kid(client)
        csrf = sign_in(client, "kid", "kid-password")

        routes = [route for route in client.app.routes if isinstance(route, APIRoute)]
        assert routes
        checked = 0
        for route in routes:
            if not (route.methods & MUTATING):
                continue
            path = re.sub(r"\{[^}]+\}", "1", route.path)
            if path in EXEMPT:
                continue
            for method in sorted(route.methods & MUTATING):
                response = client.request(method, path, json={}, headers={"X-CSRF-Token": csrf})
                assert response.status_code == 403, (method, path, response.text)
                assert response.json()["code"] == "read_only", (method, path, response.text)
                checked += 1
        assert checked >= 10


def test_user_can_log_out(settings, fake):
    client = TestClient(create_app(settings, adguard=fake))
    with client:
        complete_setup(client)
        create_kid(client)
        sign_in(client, "kid", "kid-password")
        assert client.get("/api/state").status_code == 200

        assert client.post("/api/logout", json={}).status_code == 200
        assert client.get("/api/state").status_code == 401


def test_user_state_is_reduced(settings, fake):
    client = TestClient(create_app(settings, adguard=fake))
    with client:
        complete_setup(client)
        create_kid(client)
        created = client.post(
            "/api/groups",
            json={"name": "Games", "services": ["roblox"]},
        )
        assert created.status_code == 200, created.text

        sign_in(client, "kid", "kid-password")
        state = client.get("/api/state").json()
        assert set(state) == {"app", "groups"}
        assert set(state["app"]) == APP_KEYS
        assert len(state["groups"]) == 1
        assert set(state["groups"][0]) == GROUP_KEYS

        listed = client.get("/api/groups").json()["groups"]
        assert set(listed[0]) == GROUP_KEYS


def test_kid_payload_has_only_the_schedule_state(settings, fake, monkeypatch):
    client = TestClient(create_app(settings, adguard=fake))
    with client:
        # frozen at local noon: the window is fixed against this instant, so the
        # verdict does not depend on when the suite runs
        fixed = datetime(2026, 11, 11, 12, 0, tzinfo=UTC).timestamp()
        monkeypatch.setattr(time, "time", lambda: fixed)
        complete_setup(client)
        create_kid(client)
        start, end = "11:55", "12:55"
        created = client.post(
            "/api/groups",
            json={
                "name": "Games",
                "services": ["roblox"],
                "windows": [{"start": start, "end": end, "days": "all"}],
            },
        )
        assert created.status_code == 200, created.text

        sign_in(client, "kid", "kid-password")
        group = client.get("/api/state").json()["groups"][0]
        assert set(group) == GROUP_KEYS
        assert group["open_now"] is True
        assert group["open_until"] == end  # a local HH:MM, never an epoch
        assert group["opens_at"] is None
        assert group["status"] == "allowed"
        assert group["permitted"] is True
        assert group["id"] and group["name"] == "Games" and group["emoji"] == ""
        assert group["paused"] is False

        # the window itself is admin-only, in both the state and the group list
        for payload in (group, client.get("/api/groups").json()["groups"][0]):
            for hidden in ("windows", "start_min", "end_min", "days", "services", "domains"):
                assert hidden not in payload


def test_kid_payload_names_the_next_opening(settings, fake, monkeypatch):
    client = TestClient(create_app(settings, adguard=fake))
    with client:
        fixed = datetime(2026, 11, 11, 12, 0, tzinfo=UTC).timestamp()
        monkeypatch.setattr(time, "time", lambda: fixed)
        complete_setup(client)
        create_kid(client)
        created = client.post(
            "/api/groups",
            json={
                "name": "Games",
                "services": ["roblox"],
                "windows": [{"start": "15:00", "end": "16:00", "days": "weekday"}],
            },
        )
        assert created.status_code == 200, created.text

        sign_in(client, "kid", "kid-password")
        group = client.get("/api/state").json()["groups"][0]
        assert set(group) == GROUP_KEYS
        assert group["status"] == "outside_hours"
        assert group["permitted"] is False
        assert group["open_now"] is False
        assert group["opens_at"] == "15:00"
        assert group["open_until"] is None
        # no epoch leaks into the kid payload: only a free window carries one
        assert group["free_until"] is None


def test_user_cannot_change_group_windows(settings, fake):
    client = TestClient(create_app(settings, adguard=fake))
    with client:
        complete_setup(client)
        create_kid(client)
        created = client.post(
            "/api/groups",
            json={
                "name": "Games",
                "services": ["roblox"],
                "windows": [{"start": "21:00", "end": "07:00", "days": "all"}],
            },
        )
        assert created.status_code == 200, created.text
        group_id = created.json()["group"]["id"]

        csrf = sign_in(client, "kid", "kid-password")
        response = client.put(
            f"/api/groups/{group_id}",
            json={"name": "Games", "windows": []},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "read_only"

        sign_in(client, "admin", ADMIN2["password"])
        windows = client.get("/api/state").json()["groups"][0]["windows"]
        assert len(windows) == 1


def test_user_sees_only_own_and_shared_groups(settings, fake):
    client = TestClient(create_app(settings, adguard=fake))
    with client:
        complete_setup(client)
        kid1 = create_kid(client, "kid1", "kid1-password")
        kid2 = create_kid(client, "kid2", "kid2-password")

        for name, owner, service in (
            ("Kid1 games", kid1["id"], "roblox"),
            ("Kid2 games", kid2["id"], "youtube"),
        ):
            created = client.post(
                "/api/groups",
                json={
                    "name": name,
                    "services": [service],
                    "owner_id": owner,
                },
            )
            assert created.status_code == 200, created.text
        shared = client.post("/api/groups", json={"name": "Shared", "services": ["instagram"]})
        assert shared.status_code == 200, shared.text

        admin_names = {g["name"] for g in client.get("/api/state").json()["groups"]}
        assert admin_names == {"Kid1 games", "Kid2 games", "Shared"}

        sign_in(client, "kid1", "kid1-password")
        kid_names = {g["name"] for g in client.get("/api/state").json()["groups"]}
        assert kid_names == {"Kid1 games", "Shared"}


def test_admin_before_setup_lands_on_setup(settings, fake):
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        sign_in(client, "admin", "admin")
        response = client.get("/", follow_redirects=False)
        assert response.status_code in {302, 303, 307}
        assert response.headers["location"].endswith("/setup")


def test_user_lands_on_kid_view(settings, fake):
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        complete_setup(client)
        create_kid(client)
        kid = TestClient(app)
        sign_in(kid, "kid", "kid-password")
        response = kid.get("/")
        assert response.status_code == 200
        assert 'data-page="kid"' in response.text
        kid.close()


def test_default_password_hint_only_before_setup(settings, fake):
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        before = client.get("/login")
        assert "admin / admin" in before.text

        complete_setup(client)
        fresh = TestClient(app)
        after = fresh.get("/login")
        assert after.status_code == 200
        assert "admin / admin" not in after.text
        fresh.close()


def test_landing_respects_base_path(tmp_path, fake):
    from timefence.config import Settings

    settings = Settings.from_env(
        {
            "ADGUARD_URL": "http://127.0.0.1:3000",
            "ADGUARD_USER": "admin",
            "ADGUARD_PASSWORD": "secret",
            "BASE_PATH": "/timer",
            "TZ": "UTC",
            "DATA_DIR": str(tmp_path / "data"),
            "SECURE_COOKIES": "false",
            "RECONCILE_INTERVAL": "60",
            "LOG_LEVEL": "WARNING",
        }
    )
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        anonymous = client.get("/timer/", follow_redirects=False)
        assert anonymous.status_code in {302, 303, 307}
        assert anonymous.headers["location"].endswith("/timer/login")

        login = client.post("/timer/api/login", json={"username": "admin", "password": "admin"})
        assert login.status_code == 200, login.text
        before_setup = client.get("/timer/", follow_redirects=False)
        assert before_setup.headers["location"].endswith("/timer/setup")


def test_static_js_has_no_removed_endpoints():
    from timefence import api

    static = Path(api.__file__).parent / "static"
    banned = ("/api/session/", "/api/admin/credentials")
    hits: list[tuple[str, str]] = []
    for path in sorted(static.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        for token in banned:
            if token in text:
                hits.append((path.name, token))
    assert hits == []


def test_app_html_has_no_session_or_legacy_credentials_ui():
    from timefence import api

    templates = Path(api.__file__).parent / "templates"
    html = (templates / "app.html").read_text(encoding="utf-8")
    banned = ('id="pin-form"', 'id="password-form"', "sessions-table", "history.sessions_today")
    for token in banned:
        assert token not in html, token


def _settings_for(tmp_path, base_path):
    from timefence.config import Settings

    return Settings.from_env(
        {
            "ADGUARD_URL": "http://127.0.0.1:3000",
            "ADGUARD_USER": "admin",
            "ADGUARD_PASSWORD": "secret",
            "BASE_PATH": base_path,
            "TZ": "UTC",
            "DATA_DIR": str(tmp_path / "data"),
            "SECURE_COOKIES": "false",
            "RECONCILE_INTERVAL": "60",
            "LOG_LEVEL": "WARNING",
        }
    )


def _assert_page_urls(html: str, base: str, script: str) -> None:
    assert f'href="{base}/static/style.css"' in html
    assert f'href="{base}/static/favicon.svg"' in html
    assert f'src="{base}/static/{script}"' in html
    assert f'data-base="{base}"' in html
    # no protocol-relative URL may ever reach the browser
    assert 'href="//' not in html
    assert 'src="//' not in html


@pytest.mark.parametrize("prefix", ["/", "/tf"])
def test_pages_build_urls_for_root_and_prefix(tmp_path, fake, prefix):
    app = create_app(_settings_for(tmp_path, prefix), adguard=fake)
    base = "" if prefix == "/" else prefix

    with TestClient(app) as client:

        def api(path: str) -> str:
            return f"{base}{path}"

        def login(client, username: str, password: str) -> None:
            response = client.post(
                api("/api/login"), json={"username": username, "password": password}
            )
            assert response.status_code == 200, response.text
            client.headers.update({"X-CSRF-Token": response.json()["csrf"]})

        root = client.get(api("/"), follow_redirects=False)
        assert root.status_code in {302, 303, 307}
        location = root.headers["location"]
        assert location == f"{base}/login", location
        assert "//" not in location
        if base:
            no_slash = client.get(base, follow_redirects=False)
            assert no_slash.status_code in {302, 303, 307}
            assert no_slash.headers["location"] == f"{base}/"

        login_page = client.get(api("/login"))
        assert login_page.status_code == 200
        _assert_page_urls(login_page.text, base, "auth.js")

        login(client, "admin", "admin")
        before_setup = client.get(api("/"), follow_redirects=False)
        assert before_setup.headers["location"] == f"{base}/setup"
        assert "//" not in before_setup.headers["location"]
        setup_page = client.get(api("/setup"))
        _assert_page_urls(setup_page.text, base, "auth.js")

        done = client.post(api("/api/setup"), json={"admin_password": "correct horse battery"})
        assert done.status_code == 200, done.text
        login(client, "admin", "correct horse battery")
        admin_page = client.get(api("/"))
        assert admin_page.status_code == 200
        assert 'data-page="app"' in admin_page.text
        _assert_page_urls(admin_page.text, base, "app.js")

        created = client.post(
            api("/api/users"),
            json={"username": "kid", "password": "kid-password", "role": "user"},
        )
        assert created.status_code == 200, created.text

        kid = TestClient(app)
        login(kid, "kid", "kid-password")
        kid_page = kid.get(api("/"))
        assert 'data-page="kid"' in kid_page.text
        _assert_page_urls(kid_page.text, base, "kid.js")
        assert kid.get(api("/static/style.css")).status_code == 200
        kid.close()

        assert client.get(api("/static/style.css")).status_code == 200
        assert client.get(api("/login")).status_code == 200


def _app_js() -> str:
    from timefence import api

    return (Path(api.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")


def test_poll_refresh_keeps_focus_and_typed_input():
    """No DOM churn on an unchanged poll, and focus survives a refresh that does change it.

    This cannot observe a real browser; the browser pre-check is the authoritative
    evidence. It fails if the early return, the focus snapshot, the node-order
    guard or the live-markup diff is removed.
    """
    text = _app_js()
    required = [
        "var renderedSignature = null;",
        "if (signature === renderedSignature) { return; }",
        "var focus = captureFocus();",
        "restoreFocus(focus);",
        "card.previousElementSibling !== previous",
        "live.dataset.html !== liveMarkup",
        "target.setSelectionRange(saved.start, saved.end);",
    ]
    missing = [token for token in required if token not in text]
    assert missing == [], missing
    assert "container.appendChild(card);  /* keeps the order in step with the data */" not in text


def test_dialog_traps_focus_and_returns_it_to_the_opener():
    """The dialog remembers its opener, traps Tab and is labelled by its heading."""
    text = _app_js()
    required = [
        "var modalOpener = null;",
        'if (modal.classList.contains("hidden")) { modalOpener = document.activeElement; }',
        'modal.setAttribute("aria-labelledby", heading.id);',
        "if (opener && opener.focus && document.contains(opener)) { opener.focus(); }",
        'modal.removeAttribute("aria-labelledby");',
        "function modalFocusable()",
        'if (event.key !== "Tab") { return; }',
        "if (event.shiftKey && (active === first || !inside)) {",
        "} else if (!event.shiftKey && (active === last || !inside)) {",
    ]
    missing = [token for token in required if token not in text]
    assert missing == [], missing


def test_user_cannot_see_or_change_alerts(settings, fake):
    client = TestClient(create_app(settings, adguard=fake))
    with client:
        complete_setup(client)
        create_kid(client)
        assert client.get("/api/alerts").status_code == 200

        csrf = sign_in(client, "kid", "kid-password")
        assert client.get("/api/alerts").status_code == 403
        put = client.put("/api/alerts", json={"channel": "ntfy"}, headers={"X-CSRF-Token": csrf})
        assert put.status_code == 403
        assert put.json()["code"] == "read_only"
        test = client.post("/api/alerts/test", json={}, headers={"X-CSRF-Token": csrf})
        assert test.status_code == 403
        assert test.json()["code"] == "read_only"


def _kid_js() -> str:
    from timefence import api

    return (Path(api.__file__).parent / "static" / "kid.js").read_text(encoding="utf-8")


def test_the_schedule_strings_cover_open_and_closed():
    """The card and the kid page name the schedule; no budget sentence is left."""
    app = _app_js()
    kid = _kid_js()
    assert 't("status.open_until", { time: group.open_until })' in app
    assert 't("status.open_all_day")' in app
    assert 't("status.opens_at", { time: group.opens_at || "" })' in app
    assert 't("kid.open_until", { time: group.open_until })' in kid
    assert 't("kid.open_all_day")' in kid
    assert 't("kid.opens_at", { time: group.opens_at || "" })' in kid
    for key in (
        "status.open_until",
        "status.open_all_day",
        "status.opens_at",
        "kid.open_until",
        "kid.open_all_day",
        "kid.opens_at",
    ):
        assert key in i18n.STRINGS["en"], key
        assert i18n.t(key) != key, key
    assert "{time}" in i18n.STRINGS["en"]["status.open_until"]
    assert "{time}" in i18n.STRINGS["en"]["status.opens_at"]
    assert "{time}" not in i18n.STRINGS["en"]["status.open_all_day"]
    assert "{time}" not in i18n.STRINGS["en"]["kid.open_all_day"]


def test_alert_clear_controls_send_an_empty_string():
    from timefence import api

    templates = Path(api.__file__).parent / "templates"
    html = (templates / "app.html").read_text(encoding="utf-8")
    assert 'data-action="clear-secret"' in html
    assert 'data-secret="ntfy_token"' in html
    assert 'data-secret="webhook_url"' in html
    assert 'payload[secret] = "";' in _app_js()
    for key in ("alerts.clear", "alerts.clear_token", "alerts.clear_webhook"):
        assert key in i18n.STRINGS["en"]
