"""HTTP flows: users, forced setup, CSRF, groups, guard, fail-safe."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from timefence import i18n
from timefence.api import create_app

from .conftest import FakeAdGuard, locked_conn, make_entry
from .fakes import StubServer

ADMIN = {"username": "admin", "password": "admin"}
ADMIN2 = {"username": "admin", "password": "correct horse battery"}


def login(client, username, password):
    response = client.post("/api/login", json={"username": username, "password": password})
    if response.status_code == 200:
        client.headers.update({"X-CSRF-Token": response.json()["csrf"]})
    return response


def complete_setup(client):
    login(client, **ADMIN)
    assert client.post("/api/setup", json={"admin_password": ADMIN2["password"]}).status_code == 200
    login(client, **ADMIN2)
    return client


def make_group(client, **overrides):
    payload = {"name": "Games", "services": ["roblox"]}
    payload.update(overrides)
    response = client.post("/api/groups", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["group"]


# --------------------------------------------------------------------------- #
# first-run setup and login
# --------------------------------------------------------------------------- #


def test_before_setup_only_login_and_setup_work(client):
    assert client.get("/api/state").status_code == 403
    assert (
        client.post("/api/login", json={"username": "nobody", "password": "x"}).status_code == 401
    )
    response = login(client, **ADMIN)
    assert response.status_code == 200
    assert response.json()["role"] == "admin"
    assert client.get("/api/meta").json()["setup_complete"] is False
    assert client.get("/api/state").status_code == 403  # still gated


def test_setup_validates_and_completes(admin):
    client = admin["client"]
    assert client.post("/api/setup", json={"admin_password": "admin"}).status_code == 400
    assert client.post("/api/setup", json={"admin_password": "short"}).status_code == 400
    assert client.post("/api/setup", json={"admin_password": ADMIN2["password"]}).status_code == 200
    assert client.get("/api/meta").json()["setup_complete"] is True
    assert client.post("/api/login", json=ADMIN).status_code == 401
    assert login(client, **ADMIN2).status_code == 200
    assert client.get("/api/state").status_code == 200


def test_static_files_are_revalidated_so_an_upgrade_is_not_served_stale(ready):
    """A browser keeps heuristically cached CSS/JS across an upgrade otherwise."""
    client = ready["client"]
    for name in ("style.css", "app.js", "kid.js"):
        response = client.get(f"/static/{name}")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers.get("etag")
    css = client.get("/static/style.css")
    revalidated = client.get("/static/style.css", headers={"If-None-Match": css.headers["etag"]})
    assert revalidated.status_code == 304
    assert revalidated.headers["cache-control"] == "no-cache"


def test_a_group_needs_a_service_or_a_domain(ready):
    client = ready["client"]
    created = client.post("/api/groups", json={"name": "Nothing"})
    assert created.status_code == 400
    assert created.json()["code"] == "no_targets"
    empty_lists = client.post(
        "/api/groups",
        json={"name": "Nothing", "services": [], "domains": []},
    )
    assert empty_lists.status_code == 400
    blank_text = client.post("/api/groups", json={"name": "Nothing", "domains_text": "  \n "})
    assert blank_text.status_code == 400
    assert client.get("/api/state").json()["groups"] == []


def test_emptying_the_last_service_and_domain_is_refused(ready):
    client = ready["client"]
    group = make_group(client, services=[], domains=["only.example"])
    gid = group["id"]
    for emptying in ({"domains": []}, {"domains_text": ""}):
        response = client.put(f"/api/groups/{gid}", json={"name": group["name"], **emptying})
        assert response.status_code == 400, emptying
        assert response.json()["code"] == "no_targets"
    kept = next(g for g in client.get("/api/state").json()["groups"] if g["id"] == gid)
    assert kept["domains"] == ["only.example"]


def test_an_empty_domain_list_clears_the_domains(ready):
    client = ready["client"]
    group = make_group(client, services=["roblox"], domains=["extra.example"])
    gid = group["id"]
    cleared = client.put(
        f"/api/groups/{gid}", json={"name": group["name"], "services": ["roblox"], "domains": []}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["group"]["domains"] == []
    assert cleared.json()["group"]["services"] == ["roblox"]
    again = client.put(
        f"/api/groups/{gid}",
        json={"name": group["name"], "services": ["roblox"], "domains_text": "back.example"},
    )
    assert again.json()["group"]["domains"] == ["back.example"]


def test_an_update_that_leaves_out_services_or_domains_keeps_them(ready):
    client = ready["client"]
    group = make_group(client, services=["roblox"], domains=["extra.example"])
    renamed = client.put(f"/api/groups/{group['id']}", json={"name": "Renamed"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["group"]["services"] == ["roblox"]
    assert renamed.json()["group"]["domains"] == ["extra.example"]


def test_csrf_is_required_for_writes(ready):
    client = ready["client"]
    response = client.post("/api/groups/1/block", json={}, headers={"X-CSRF-Token": "x"})
    assert response.status_code == 403
    assert response.json()["code"] == "csrf"


def test_login_rate_limit_locks_the_client(settings):
    app = create_app(settings, adguard=FakeAdGuard())
    with TestClient(app) as client:
        complete_setup(client)
        for _ in range(4):
            assert login(client, "admin", "nope").status_code == 401
        assert login(client, "admin", "nope").status_code == 429
        assert login(client, **ADMIN2).status_code == 429


# --------------------------------------------------------------------------- #
# users
# --------------------------------------------------------------------------- #


def test_user_crud_and_guards(ready):
    client = ready["client"]
    created = client.post(
        "/api/users", json={"username": "kid", "password": "kid-password", "role": "user"}
    )
    assert created.status_code == 200, created.text
    kid = created.json()["user"]
    assert kid["role"] == "user"

    assert (
        client.post(
            "/api/users", json={"username": "kid", "password": "x" * 10, "role": "user"}
        ).status_code
        == 409
    )
    assert (
        client.post("/api/users", json={"username": "bad name", "password": "x" * 10}).status_code
        == 400
    )
    assert (
        client.post("/api/users", json={"username": "shortpw", "password": "123"}).status_code
        == 400
    )

    users = client.get("/api/users").json()["users"]
    assert {u["username"] for u in users} == {"admin", "kid"}

    # promote + demote
    assert client.post(f"/api/users/{kid['id']}/role", json={"role": "admin"}).status_code == 200
    assert client.post(f"/api/users/{kid['id']}/role", json={"role": "user"}).status_code == 200

    # can't demote/disable/delete the last admin (only admin is 'admin' and it is ourselves)
    me = [u for u in users if u["username"] == "admin"][0]
    assert client.post(f"/api/users/{me['id']}/role", json={"role": "user"}).status_code == 409
    assert (
        client.post(f"/api/users/{me['id']}/disabled", json={"disabled": True}).status_code == 409
    )
    assert client.delete(f"/api/users/{me['id']}").status_code == 409

    # disable the kid, delete it
    assert (
        client.post(f"/api/users/{kid['id']}/disabled", json={"disabled": True}).status_code == 200
    )
    assert login(client, "kid", "kid-password").status_code == 401
    assert client.delete(f"/api/users/{kid['id']}").status_code == 200
    assert [u["username"] for u in client.get("/api/users").json()["users"]] == ["admin"]


def test_normal_user_cannot_use_admin_endpoints(ready):
    client = ready["client"]
    client.post("/api/users", json={"username": "kid", "password": "kid-password", "role": "user"})
    user = login(client, "kid", "kid-password")
    assert user.status_code == 200
    headers = {"X-CSRF-Token": user.json()["csrf"]}
    assert client.get("/api/state", headers=headers).status_code == 200
    assert client.get("/api/groups", headers=headers).status_code == 200
    assert client.get("/api/users", headers=headers).status_code == 403
    assert (
        client.post(
            "/api/groups",
            json={"name": "x", "domains_text": "x.com"},
            headers=headers,
        ).status_code
        == 403
    )


# --------------------------------------------------------------------------- #
# groups and modes
# --------------------------------------------------------------------------- #


def test_auto_mode_is_allowed_by_default(ready, fake):
    client = ready["client"]
    group = client.post("/api/groups", json={"name": "YouTube", "services": ["youtube"]}).json()[
        "group"
    ]
    assert group["permitted"] is True
    assert fake.ids == []  # nothing blocked: ads only
    assert fake.rules == []


def test_session_mode_is_rejected(ready):
    client = ready["client"]
    created = client.post(
        "/api/groups",
        json={"name": "YouTube", "services": ["youtube"], "mode": "session"},
    )
    assert created.status_code == 400
    assert created.json()["code"] == "mode_removed"

    group = client.post("/api/groups", json={"name": "YouTube", "services": ["youtube"]}).json()[
        "group"
    ]
    updated = client.put(
        f"/api/groups/{group['id']}",
        json={"name": "YouTube", "mode": "session"},
    )
    assert updated.status_code == 400
    assert updated.json()["code"] == "mode_removed"


def test_session_endpoints_are_gone(ready):
    client = ready["client"]
    group = make_group(client)
    started = client.post("/api/session/start", json={"group_id": group["id"], "minutes": 15})
    stopped = client.post("/api/session/stop", json={"group_id": group["id"]})
    assert started.status_code in {404, 405}
    assert stopped.status_code in {404, 405}


def test_state_has_no_session_fields(ready):
    client = ready["client"]
    make_group(client)
    state = client.get("/api/state").json()
    assert "sessions_today" not in state
    assert "session_options" not in state["app"]
    for group in state["groups"]:
        assert "active_session" not in group
        assert "mode" not in group
    assert "session_options" not in client.get("/api/meta").json()


def test_group_crud(ready, fake, monkeypatch):
    client = ready["client"]
    # frozen at UTC noon: the window below stays ahead of "now" whatever the
    # wall clock says, so the verdict does not depend on when the suite runs.
    # The session is re-issued at the frozen instant: a cookie signed at the
    # real clock is "not yet valid" once time is moved backwards.
    fixed = datetime(2026, 11, 11, 12, 0, tzinfo=UTC).timestamp()
    monkeypatch.setattr(time, "time", lambda: fixed)
    login = client.post("/api/login", json=ADMIN2)
    assert login.status_code == 200, login.text
    client.headers.update({"X-CSRF-Token": login.json()["csrf"]})
    group = make_group(client, name="YouTube", emoji="▶️", services=["youtube"])
    windows = [{"start": "13:00", "end": "14:00", "days": "all"}]
    updated = client.put(
        f"/api/groups/{group['id']}",
        json={"name": "YouTube", "services": ["youtube"], "windows": windows},
    )
    assert updated.status_code == 200
    assert updated.json()["group"]["windows"] == windows
    assert updated.json()["group"]["emoji"] == "▶️"
    assert fake.ids == ["youtube"]  # outside 13:00-14:00

    cleared = client.put(
        f"/api/groups/{group['id']}",
        json={"name": "YouTube", "services": ["youtube"], "windows": []},
    )
    assert cleared.status_code == 200
    assert fake.ids == []  # no windows: always allowed

    assert client.post(f"/api/groups/{group['id']}/pause", json={"paused": True}).status_code == 200
    assert fake.ids == ["youtube"]

    assert client.delete(f"/api/groups/{group['id']}").status_code == 200
    assert fake.ids == []


def test_domains_are_normalised_and_unique(ready, fake):
    client = ready["client"]
    first = client.post(
        "/api/groups",
        json={
            "name": "Games",
            "domains_text": "https://Example-Game.com/play?x=1",
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["group"]["domains"] == ["example-game.com"]
    assert fake.rules == []  # allowed, so nothing is blocked yet

    client.post(f"/api/groups/{first.json()['group']['id']}/block", json={})
    assert "||example-game.com^" in fake.rules

    second = client.post(
        "/api/groups",
        json={"name": "Other", "domains_text": "example-game.com"},
    )
    assert second.status_code == 400
    assert "already used" in second.json()["error"]


def test_group_windows_round_trip_and_validation(ready, fake):
    client = ready["client"]
    windows = [{"start": "21:00", "end": "07:00", "days": "all"}]

    group = make_group(client, windows=windows)
    assert group["windows"] == windows
    assert client.get("/api/state").json()["groups"][0]["windows"] == windows

    # an update that omits the key keeps the windows, an empty list clears them
    kept = client.put(f"/api/groups/{group['id']}", json={"name": "Games", "services": ["roblox"]})
    assert kept.status_code == 200, kept.text
    assert kept.json()["group"]["windows"] == windows
    cleared = client.put(
        f"/api/groups/{group['id']}",
        json={"name": "Games", "services": ["roblox"], "windows": []},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["group"]["windows"] == []

    def reject(payload):
        response = client.post(
            "/api/groups",
            json={"name": "Other", "services": ["roblox"], **payload},
        )
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "invalid_window"

    reject({"windows": [{"start": "21:00", "end": "21:00", "days": "all"}]})
    reject({"windows": [{"start": "9:00", "end": "10:00", "days": "all"}]})
    reject({"windows": [{"start": "09:00", "end": "10:00", "days": "daily"}]})
    reject(
        {
            "windows": [
                {"start": "06:00", "end": "07:00", "days": "all"},
                {"start": "12:00", "end": "13:00", "days": "all"},
                {"start": "15:00", "end": "16:00", "days": "all"},
                {"start": "18:00", "end": "19:00", "days": "all"},
                {"start": "21:00", "end": "22:00", "days": "all"},
            ]
        }
    )


def test_status_and_permitted_agree(ready, fake, monkeypatch):
    client = ready["client"]
    # frozen at UTC noon: the window is minted against this instant, so the
    # result does not depend on when the suite runs
    fixed = datetime(2026, 11, 11, 12, 0, tzinfo=UTC).timestamp()
    monkeypatch.setattr(time, "time", lambda: fixed)
    login = client.post("/api/login", json=ADMIN2)
    assert login.status_code == 200, login.text
    client.headers.update({"X-CSRF-Token": login.json()["csrf"]})
    start, end = "11:55", "12:55"
    group = make_group(client, windows=[{"start": start, "end": end, "days": "all"}])

    state = client.get("/api/state").json()["groups"][0]
    listed = client.get("/api/groups").json()["groups"][0]
    assert state["status"] == "allowed"
    assert state["permitted"] is True
    assert state["permitted"] == (state["status"] in {"allowed", "free"})
    assert state["open_now"] is True
    assert state["open_until"] == end  # a local HH:MM, never an epoch
    assert state["opens_at"] is None
    assert state["next_change"] == int(datetime(2026, 11, 11, 12, 55, tzinfo=UTC).timestamp())
    assert (listed["status"], listed["permitted"]) == (state["status"], state["permitted"])

    created = client.post(
        "/api/users", json={"username": "kid", "password": "kid-password", "role": "user"}
    )
    assert created.status_code == 200, created.text
    login = client.post("/api/login", json={"username": "kid", "password": "kid-password"})
    assert login.status_code == 200, login.text
    kid_total = {"X-CSRF-Token": login.json()["csrf"]}
    kid_state = client.get("/api/state", headers=kid_total).json()["groups"][0]
    assert (kid_state["status"], kid_state["permitted"]) == (state["status"], state["permitted"])
    assert kid_state["open_until"] == end
    assert set(kid_state) == {
        "id",
        "name",
        "emoji",
        "status",
        "permitted",
        "open_now",
        "opens_at",
        "open_until",
        "free_until",
        "paused",
    }
    assert group["windows"] == [{"start": start, "end": end, "days": "all"}]


def test_outside_hours_points_at_the_next_opening(ready, fake, monkeypatch):
    client = ready["client"]
    fixed = datetime(2026, 11, 11, 12, 0, tzinfo=UTC).timestamp()
    monkeypatch.setattr(time, "time", lambda: fixed)
    login = client.post("/api/login", json=ADMIN2)
    assert login.status_code == 200, login.text
    client.headers.update({"X-CSRF-Token": login.json()["csrf"]})
    make_group(client, windows=[{"start": "15:00", "end": "16:00", "days": "all"}])

    state = client.get("/api/state").json()["groups"][0]
    assert state["status"] == "outside_hours"
    assert state["permitted"] is False
    assert state["open_now"] is False
    assert state["opens_at"] == "15:00"
    assert state["open_until"] is None
    assert state["blocked_until"] == int(datetime(2026, 11, 11, 15, 0, tzinfo=UTC).timestamp())
    assert fake.ids == ["roblox"]  # outside the window: blocked right away


def test_critical_domain_needs_confirmation(ready):
    client = ready["client"]
    response = client.post(
        "/api/groups",
        json={"name": "AppStore", "domains_text": "apps.apple.com"},
    )
    assert response.status_code == 409
    payload = response.json()
    assert payload["code"] == "critical_domains"
    assert payload["critical"][0]["domain"] == "apps.apple.com"
    assert "App Store" in payload["critical"][0]["note"]

    confirmed = client.post(
        "/api/groups",
        json={
            "name": "AppStore",
            "domains_text": "apps.apple.com",
            "confirm_critical": True,
        },
    )
    assert confirmed.status_code == 200


def test_discover_hides_noise_and_managed_domains(ready, fake):
    client = ready["client"]
    client.post("/api/groups", json={"name": "Games", "domains_text": "known.com"})
    client.post("/api/groups", json={"name": "Roblox", "services": ["roblox"]})
    fake.entries = [
        make_entry("api.example-game.com"),
        make_entry("ssl.google-analytics.com"),
        make_entry("already.known.com"),
        make_entry("www.youtube.com"),
        make_entry("assetdelivery.roblox.com"),
        make_entry("cdn.other-site.net"),
    ]
    data = client.get("/api/discover?minutes=15").json()
    names = [d["domain"] for d in data["domains"]]
    assert "example-game.com" in names
    assert "other-site.net" in names
    assert "google-analytics.com" not in names
    assert "known.com" not in names
    assert "roblox.com" not in names
    assert "youtube.com" in names

    all_data = client.get("/api/discover?minutes=15&show_all=true").json()
    assert "google-analytics.com" in [d["domain"] for d in all_data["domains"]]


def test_test_endpoint_reports_blocked_hosts(ready, fake):
    client = ready["client"]
    group = make_group(client, services=["roblox"])
    client.post(f"/api/groups/{group['id']}/block", json={})
    results = client.get(f"/api/groups/{group['id']}/test").json()["results"]
    hosts = {r["host"]: r["blocked"] for r in results}
    assert hosts["roblox.com"] is True


# --------------------------------------------------------------------------- #
# quick actions
# --------------------------------------------------------------------------- #


def test_block_now_sets_a_force_block(ready, fake):
    client = ready["client"]
    group = client.post("/api/groups", json={"name": "YouTube", "services": ["youtube"]}).json()[
        "group"
    ]
    assert fake.ids == []  # auto mode: allowed
    client.post(f"/api/groups/{group['id']}/block", json={})
    assert fake.ids == ["youtube"]
    state = client.get("/api/state").json()["groups"][0]
    assert state["force_block_until"] > 0
    assert state["permitted"] is False

    client.post(f"/api/groups/{group['id']}/force/clear", json={})
    assert fake.ids == []
    assert client.get("/api/state").json()["groups"][0]["permitted"] is True


def test_healthz_and_meta_are_public(client):
    assert client.get("/healthz").json()["ok"] is True
    meta = client.get("/api/meta").json()
    assert meta["version"]
    assert "unofficial" in meta


def test_extra_time_until_and_clear_move_the_group_and_adguard(ready, fake, monkeypatch):
    """The existing /free and /free/clear routes, driven the way the card does."""
    client = ready["client"]
    fixed = int(datetime(2026, 11, 11, 12, 0, tzinfo=UTC).timestamp())
    monkeypatch.setattr(time, "time", lambda: fixed)
    # the session clock is the patched one too, so sign in again for a live CSRF token
    assert login(client, **ADMIN2).status_code == 200
    group = make_group(client, windows=[{"start": "15:00", "end": "16:00", "days": "all"}])
    gid = group["id"]
    assert client.get("/api/state").json()["groups"][0]["status"] == "outside_hours"
    assert fake.ids == ["roblox"]

    # a time later today is honoured as today
    allowed = client.post(f"/api/groups/{gid}/free", json={"until": "13:30"})
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["until"] == int(datetime(2026, 11, 11, 13, 30, tzinfo=UTC).timestamp())
    state = client.get("/api/state").json()["groups"][0]
    assert (state["status"], state["permitted"]) == ("free", True)
    assert state["free_until"] == allowed.json()["until"]
    assert fake.ids == []  # free lifts the block outside the window

    cleared = client.post(f"/api/groups/{gid}/free/clear")
    assert cleared.status_code == 200, cleared.text
    state = client.get("/api/state").json()["groups"][0]
    assert (state["status"], state["permitted"]) == ("outside_hours", False)
    assert state["free_until"] is None
    assert fake.ids == ["roblox"]  # the schedule blocks again

    # a time that already passed today belongs to tomorrow
    past = client.post(f"/api/groups/{gid}/free", json={"until": "11:00"})
    assert past.status_code == 200, past.text
    tomorrow = int(datetime(2026, 11, 12, 11, 0, tzinfo=UTC).timestamp())
    assert past.json()["until"] == tomorrow


def test_a_kid_sees_the_free_time_but_no_admin_fields(ready):
    """The reduced payload carries free_until; the card has no buttons to press."""
    client = ready["client"]
    created = client.post(
        "/api/users", json={"username": "kid", "password": "kid-password", "role": "user"}
    )
    assert created.status_code == 200, created.text
    group = make_group(client)
    free = client.post(f"/api/groups/{group['id']}/free", json={"until": "23:00"})
    assert free.status_code == 200, free.text
    login(client, "kid", "kid-password")
    kid_state = client.get("/api/state").json()["groups"][0]
    assert kid_state["status"] == "free"
    assert kid_state["free_until"] is not None
    for hidden in ("windows", "force_block_until", "services", "domains", "owner_id"):
        assert hidden not in kid_state


# --------------------------------------------------------------------------- #
# the budget, usage endpoints and history are gone
# --------------------------------------------------------------------------- #

GROUP_KEYS = {
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
    "owner",
}


def test_removed_budget_routes_are_gone(ready):
    client = ready["client"]
    group = make_group(client)
    removed = (
        ("GET", "/api/usage"),
        ("GET", "/api/usage.csv"),
        ("POST", f"/api/groups/{group['id']}/bonus"),
        ("POST", f"/api/groups/{group['id']}/reset"),
    )
    for method, path in removed:
        response = client.request(method, path, json={})
        assert response.status_code == 404, (method, path, response.text)


def test_legacy_minute_fields_are_ignored(ready):
    """A stored client may still send the removed minute fields; they are ignored."""
    client = ready["client"]
    response = client.post(
        "/api/groups",
        json={
            "name": "Legacy",
            "services": ["roblox"],
            "weekday_minutes": 30,
            "weekend_minutes": 45,
        },
    )
    assert response.status_code == 200, response.text
    group = response.json()["group"]
    assert not {"weekday_minutes", "weekend_minutes"} & set(group)
    assert group["windows"] == []


def test_the_state_has_no_usage_history(ready):
    client = ready["client"]
    make_group(client)
    state = client.get("/api/state").json()
    assert "history" not in state
    assert "groups" in state


def test_the_group_payload_is_the_documented_shape(ready):
    client = ready["client"]
    make_group(client, emoji="🎮", windows=[{"start": "13:00", "end": "14:00", "days": "all"}])
    group = client.get("/api/state").json()["groups"][0]
    assert set(group) == GROUP_KEYS
    assert group["owner"] is None
    assert group["windows"] == [{"start": "13:00", "end": "14:00", "days": "all"}]
    for gone in (
        "weekday_minutes",
        "weekend_minutes",
        "minutes_today",
        "bonus_minutes",
        "used_minutes",
        "remaining_minutes",
        "bedtime_active",
        "bedtime_until",
    ):
        assert gone not in group, gone


def test_block_now_lasts_until_local_midnight_on_dst_day(tmp_path, fake, monkeypatch):
    import types
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from timefence import api as api_module
    from timefence import budget as budget_module
    from timefence.config import Settings

    ny = ZoneInfo("America/New_York")
    fixed = int(datetime(2026, 11, 1, 12, 0, tzinfo=ny).timestamp())
    fake_time = types.SimpleNamespace(time=lambda: fixed)
    monkeypatch.setattr(api_module, "time", fake_time)
    monkeypatch.setattr(budget_module, "time", fake_time)

    settings = Settings.from_env(
        {
            "ADGUARD_URL": "http://127.0.0.1:3000",
            "ADGUARD_USER": "admin",
            "ADGUARD_PASSWORD": "secret",
            "BASE_PATH": "/",
            "TZ": "America/New_York",
            "DATA_DIR": str(tmp_path / "data"),
            "SECURE_COOKIES": "false",
            "RECONCILE_INTERVAL": "60",
            "LOG_LEVEL": "WARNING",
        }
    )
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        complete_setup(client)
        created = client.post(
            "/api/groups",
            json={"name": "YouTube", "services": ["youtube"]},
        )
        assert created.status_code == 200, created.text
        group_id = created.json()["group"]["id"]
        assert client.post(f"/api/groups/{group_id}/block", json={}).status_code == 200
        groups = client.get("/api/state").json()["groups"]

    blocked_until = [row for row in groups if row["id"] == group_id][0]["force_block_until"]
    expected = int(datetime(2026, 11, 2, 0, 0, tzinfo=ny).timestamp())
    assert blocked_until == expected


# --------------------------------------------------------------------------- #
# alerts
# --------------------------------------------------------------------------- #


def test_alert_config_is_masked_merged_and_validated(ready):
    client = ready["client"]
    response = client.get("/api/alerts")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    shown = response.json()
    assert shown["channel"] == ""
    assert shown["enabled"] is True
    assert shown["events"]["group_closed"] is False
    assert shown["events"]["sync_failing"] is True
    assert shown["events"]["sync_recovered"] is True

    topic = "topic-SUPERSECRET"
    token = "token-THIS-MUST-NOT-LEAK"
    hook = "http://192.168.1.9:8080/private/hook-SECRET"
    update = client.put(
        "/api/alerts",
        json={
            "channel": "ntfy",
            "ntfy_topic": topic,
            "ntfy_token": token,
            "webhook_url": hook,
            "events": {"sync_failing": False},
        },
    )
    assert update.status_code == 200, update.text
    assert update.headers["cache-control"] == "no-store"
    shown = update.json()
    text = json.dumps(shown)
    for secret in (topic, token, hook, "/private/hook"):
        assert secret not in text
    assert shown["ntfy"]["topic_tail"] == topic[-2:]
    assert shown["ntfy"]["token_configured"] is True
    assert shown["webhook"]["path_tail"] == "ET"
    assert shown["events"]["sync_failing"] is False

    body = client.get("/api/alerts").text
    assert topic not in body and token not in body

    # an absent key keeps the stored value, "" clears it
    kept = client.put("/api/alerts", json={"events": {"group_closed": True}}).json()
    assert kept["channel"] == "ntfy"
    assert kept["ntfy"]["topic_tail"] == topic[-2:]
    assert kept["events"]["group_closed"] is True
    cleared = client.put("/api/alerts", json={"ntfy_token": ""}).json()
    assert cleared["ntfy"]["token_configured"] is False

    # switching the channel keeps the other channel's stored values
    switched = client.put("/api/alerts", json={"channel": "webhook"}).json()
    assert switched["channel"] == "webhook"
    assert switched["ntfy"]["topic_tail"] == topic[-2:]

    # config is never written to the event log
    with locked_conn(client.app) as conn:
        details = [row["detail"] or "" for row in conn.execute("SELECT detail FROM events")]
    assert all("SECRET" not in detail and "TOKEN" not in detail for detail in details)

    for bad in (
        {"channel": "email"},
        {"ntfy_topic": "bad topic!"},
        {"ntfy_base": "ftp://ntfy.example"},
        {"ntfy_token": "token with a space"},
        {"webhook_url": "http://u:p@host/hook"},
        {"events": []},
        {"events": {"group_closed": "maybe"}},
    ):
        response = client.put("/api/alerts", json=bad)
        assert response.status_code == 400, (bad, response.text)


def test_telegram_config_round_trip_and_role_gate(ready):
    client = ready["client"]
    token = "8471903:AAH2fake-Token_for_TESTS_0123456789ab"
    chat = "-1002233445566"

    response = client.put(
        "/api/alerts",
        json={"channel": "telegram", "telegram_token": token, "telegram_chat": chat},
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    shown = response.json()
    text = json.dumps(shown)
    assert token not in text
    assert chat not in text
    assert shown["channel"] == "telegram"
    assert shown["telegram"] == {"token_tail": "ab", "chat_tail": "66", "configured": True}

    body = client.get("/api/alerts").text
    assert token not in body and chat not in body

    # an absent key keeps the stored value, "" clears it
    kept = client.put("/api/alerts", json={"enabled": False}).json()
    assert kept["telegram"]["configured"] is True
    cleared = client.put("/api/alerts", json={"telegram_chat": ""}).json()
    assert cleared["telegram"]["configured"] is False
    assert cleared["telegram"]["token_tail"] == "ab"

    for bad in (
        {"telegram_token": "nope"},
        {"telegram_chat": "@someone"},
        {"telegram_chat": "1 2"},
    ):
        assert client.put("/api/alerts", json=bad).status_code == 400, bad

    # a kid account can neither read nor change the alert settings
    created = client.post(
        "/api/users", json={"username": "kid", "password": "kid-password", "role": "user"}
    )
    assert created.status_code == 200, created.text
    login = client.post("/api/login", json={"username": "kid", "password": "kid-password"})
    assert login.status_code == 200, login.text
    kid_csrf = login.json()["csrf"]
    assert client.get("/api/alerts").status_code == 403
    put = client.put(
        "/api/alerts", json={"telegram_chat": "-1"}, headers={"X-CSRF-Token": kid_csrf}
    )
    assert put.status_code == 403
    assert put.json()["code"] == "read_only"


def test_the_today_tab_has_one_alerts_setup_hint(ready):
    from timefence import i18n

    client = ready["client"]
    page = client.get("/").text
    assert 'id="alerts-hint"' in page
    assert i18n.t("alerts.hint") in page
    assert i18n.t("alerts.setup") in page

    # a kid account gets its own screen, so the admin hint never reaches it
    created = client.post(
        "/api/users", json={"username": "kid", "password": "kid-password", "role": "user"}
    )
    assert created.status_code == 200, created.text
    assert login(client, "kid", "kid-password").status_code == 200
    kid_page = client.get("/").text
    assert "alerts-hint" not in kid_page
    assert i18n.t("alerts.setup") not in kid_page


def test_alert_test_endpoint_reports_categories(ready, deliveries):
    from timefence import alerts

    client = ready["client"]
    assert client.post("/api/alerts/test", json={}).json() == {"ok": False, "category": "config"}

    assert alerts.TEST_LOCK.acquire(blocking=False)
    try:
        busy = client.post("/api/alerts/test", json={})
    finally:
        alerts.TEST_LOCK.release()
    assert busy.status_code == 429
    assert busy.json()["code"] == "busy"

    with StubServer(status=500) as stub:
        client.put("/api/alerts", json={"channel": "webhook", "webhook_url": stub.url + "/hook"})
        assert client.post("/api/alerts/test", json={}).json() == {
            "ok": False,
            "category": "http_500",
        }

    with StubServer(status=302) as stub:
        stub.location = stub.url + "/elsewhere"
        client.put("/api/alerts", json={"channel": "webhook", "webhook_url": stub.url + "/hook"})
        assert client.post("/api/alerts/test", json={}).json() == {
            "ok": False,
            "category": "http_3xx",
        }
        assert len(stub.requests) == 1

    with StubServer() as stub:
        client.put("/api/alerts", json={"channel": "webhook", "webhook_url": stub.url + "/hook"})
        answer = client.post("/api/alerts/test", json={})
    assert answer.status_code == 200
    assert answer.json() == {"ok": True}
    assert len(stub.requests) == 1
    # the test result is remembered without any body or path
    shown = client.get("/api/alerts").json()
    assert shown["last_result"] == "ok"
    assert shown["last_result_ts"]


# --------------------------------------------------------------------------- #
# input hygiene: ownership on delete, payload shapes, icons, logout, body cap
# --------------------------------------------------------------------------- #


def test_deleting_a_user_leaves_their_groups_shared(ready):
    client = ready["client"]
    kid = client.post(
        "/api/users", json={"username": "kid", "password": "kid-password", "role": "user"}
    ).json()["user"]
    kept = make_group(client, name="Kept", owner_id=kid["id"])
    gone = make_group(client, name="Gone", owner_id=kid["id"], services=["instagram"])
    assert client.delete(f"/api/groups/{gone['id']}").status_code == 200

    assert client.delete(f"/api/users/{kid['id']}").status_code == 200

    groups = {row["id"]: row for row in client.get("/api/state").json()["groups"]}
    assert groups[kept["id"]]["owner_id"] is None
    assert groups[kept["id"]]["name"] == "Kept"
    # the soft-deleted row must not keep a dangling owner either
    with locked_conn(client.app) as conn:
        owners = {row["owner_id"] for row in conn.execute("SELECT owner_id FROM groups")}
    assert owners == {None}


def test_group_payload_types_are_checked(ready):
    client = ready["client"]
    bad_services = client.post("/api/groups", json={"name": "X", "services": 5})
    assert bad_services.status_code == 400
    assert bad_services.json()["code"] == "invalid"
    bad_domains = client.post("/api/groups", json={"name": "X", "domains": "example.com"})
    assert bad_domains.status_code == 400
    assert bad_domains.json()["code"] == "invalid"

    group = make_group(client)
    too_long = client.put(
        f"/api/groups/{group['id']}", json={"name": "x" * 500, "services": ["roblox"]}
    )
    assert too_long.status_code == 400
    assert too_long.json()["code"] == "invalid"
    fresh = client.get("/api/state").json()["groups"][0]
    assert fresh["name"] == "Games"


def test_a_malformed_domain_answers_400_and_names_the_offending_text(ready):
    """A typo in the editor must be a sentence, not Starlette's plain 500."""
    client = ready["client"]
    for text in ("not a domain", "youtube.com,roblox.com", "müchen.de", " a b "):
        created = client.post("/api/groups", json={"name": "Typo", "domains_text": text})
        assert created.status_code == 400, (text, created.text)
        body = created.json()
        assert body["code"] == "invalid_domain", (text, body)
        assert body["error"] == i18n.t("error.invalid_domain", domain=text.strip())

    group = make_group(client)
    for payload in ({"domains_text": "bad domain"}, {"domains": ["bad domain"]}):
        updated = client.put(f"/api/groups/{group['id']}", json=payload)
        assert updated.status_code == 400, (payload, updated.text)
        assert updated.json()["code"] == "invalid_domain"
        assert updated.json()["error"] == i18n.t("error.invalid_domain", domain="bad domain")

    # a non-string list item used to raise AttributeError deep in budget.py
    non_string = client.post("/api/groups", json={"name": "N", "domains": [123]})
    assert non_string.status_code == 400, non_string.text
    assert non_string.json()["code"] == "invalid_domain"

    kept = next(g for g in client.get("/api/state").json()["groups"] if g["id"] == group["id"])
    assert kept["domains"] == []


def test_an_unknown_owner_answers_400(ready):
    client = ready["client"]
    created = client.post(
        "/api/groups", json={"name": "Owned", "services": ["youtube"], "owner_id": 999}
    )
    assert created.status_code == 400, created.text
    assert created.json()["code"] == "invalid_owner"
    assert created.json()["error"] == i18n.t("error.invalid_owner")

    group = make_group(client)
    for owner in (999, "nobody"):
        updated = client.put(f"/api/groups/{group['id']}", json={"owner_id": owner})
        assert updated.status_code == 400, (owner, updated.text)
        assert updated.json()["code"] == "invalid_owner"
        assert updated.json()["error"] == i18n.t("error.invalid_owner")
    assert {g["name"] for g in client.get("/api/state").json()["groups"]} == {"Games"}


def test_a_duplicate_group_name_answers_400(ready):
    """The unique index must never surface as sqlite IntegrityError."""
    client = ready["client"]
    make_group(client, name="Games")
    created = client.post("/api/groups", json={"name": "Games", "services": ["steam"]})
    assert created.status_code == 400, created.text
    assert created.json()["code"] == "duplicate_name"
    assert created.json()["error"] == i18n.t("error.duplicate_name", name="Games")

    other = make_group(client, name="Other", services=["steam"])
    updated = client.put(f"/api/groups/{other['id']}", json={"name": "Games"})
    assert updated.status_code == 400, updated.text
    assert updated.json()["code"] == "duplicate_name"
    assert updated.json()["error"] == i18n.t("error.duplicate_name", name="Games")
    assert {g["name"] for g in client.get("/api/state").json()["groups"]} == {"Games", "Other"}


def test_duplicate_services_in_one_payload_are_de_duplicated(ready, app):
    """Duplicate services must answer 200, not surface as IntegrityError (500)."""
    client = ready["client"]
    created = client.post(
        "/api/groups",
        json={"name": "Dupes", "services": ["YouTube", "youtube", " YouTube "]},
    )
    assert created.status_code == 200, created.text
    group = created.json()["group"]
    assert group["services"] == ["youtube"]
    with locked_conn(app) as conn:
        stored = conn.execute(
            "SELECT COUNT(*) FROM group_services WHERE group_id=?", (group["id"],)
        ).fetchone()[0]
    assert stored == 1

    updated = client.put(f"/api/groups/{group['id']}", json={"services": ["Roblox", "roblox"]})
    assert updated.status_code == 200, updated.text
    assert updated.json()["group"]["services"] == ["roblox"]
    with locked_conn(app) as conn:
        stored = conn.execute(
            "SELECT COUNT(*) FROM group_services WHERE group_id=?", (group["id"],)
        ).fetchone()[0]
    assert stored == 1


def test_each_window_problem_has_its_own_sentence(ready):
    client = ready["client"]
    keys = {
        "start_equals_end": "error.window_start_equals_end",
        "bad_time": "error.window_bad_time",
        "bad_days": "error.window_bad_days",
        "too_many": "error.window_too_many",
    }
    cases = {
        "start_equals_end": [{"start": "21:00", "end": "21:00", "days": "all"}],
        "bad_time": [{"start": "25:00", "end": "26:00", "days": "all"}],
        "bad_days": [{"start": "21:00", "end": "22:00", "days": "someday"}],
        "too_many": [
            {"start": "06:00", "end": "07:00", "days": "all"},
            {"start": "09:00", "end": "10:00", "days": "all"},
            {"start": "12:00", "end": "13:00", "days": "all"},
            {"start": "15:00", "end": "16:00", "days": "all"},
            {"start": "18:00", "end": "19:00", "days": "all"},
        ],
    }
    assert len({i18n.t(key) for key in keys.values()}) == 4  # four distinct sentences
    for reason, windows in cases.items():
        response = client.post(
            "/api/groups", json={"name": "W", "services": ["roblox"], "windows": windows}
        )
        assert response.status_code == 400, (reason, response.text)
        assert response.json()["code"] == "invalid_window", reason
        assert response.json()["error"] == i18n.t(keys[reason])

    group = make_group(client)
    updated = client.put(
        f"/api/groups/{group['id']}",
        json={"windows": [{"start": "21:00", "end": "21:00", "days": "all"}]},
    )
    assert updated.status_code == 400, updated.text
    assert updated.json()["code"] == "invalid_window"
    assert updated.json()["error"] == i18n.t("error.window_start_equals_end")


def test_name_and_paused_must_have_the_right_json_types(ready):
    client = ready["client"]
    for payload, key in (
        ({"name": 5, "services": ["roblox"]}, "error.name_not_text"),
        ({"name": ["x"], "services": ["roblox"]}, "error.name_not_text"),
        ({"name": "X", "services": ["roblox"], "paused": "false"}, "error.paused_not_bool"),
        ({"name": "X", "services": ["roblox"], "paused": 0}, "error.paused_not_bool"),
        (
            {"name": "X", "services": ["roblox"], "domains_text": {"a": 1}},
            "error.group_domains_text",
        ),
    ):
        response = client.post("/api/groups", json=payload)
        assert response.status_code == 400, (payload, response.text)
        assert response.json()["code"] == "invalid", payload
        assert response.json()["error"] == i18n.t(key), payload
    assert client.get("/api/state").json()["groups"] == []

    group = make_group(client)
    for payload, key in (
        ({"name": None}, "error.name_not_text"),
        ({"paused": "true"}, "error.paused_not_bool"),
    ):
        response = client.put(f"/api/groups/{group['id']}", json=payload)
        assert response.status_code == 400, (payload, response.text)
        assert response.json()["code"] == "invalid", payload
        assert response.json()["error"] == i18n.t(key), payload
    fresh = next(g for g in client.get("/api/state").json()["groups"] if g["id"] == group["id"])
    assert fresh["name"] == "Games"
    assert fresh["paused"] is False


def test_service_icons_are_sanitised(ready, fake):
    from timefence import api

    assert api.safe_icon("PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciLz4=")
    assert api.safe_icon('x" onerror="alert(1)') == ""
    assert api.safe_icon(None) == ""
    assert api.safe_icon("") == ""

    fake.SERVICES = fake.SERVICES + [
        {"id": "evil", "name": "Evil", "rules": [], "icon_svg": 'x" onerror="alert(1)'},
        {"id": "good", "name": "Good", "rules": [], "icon_svg": "PHN2Zy8+"},
    ]
    services = {row["id"]: row for row in ready["client"].get("/api/services").json()["services"]}
    assert services["evil"]["icon_svg"] == ""
    assert services["good"]["icon_svg"] == "PHN2Zy8+"


def test_logout_revokes_the_copied_cookie(ready):
    client = ready["client"]
    session = client.cookies.get("timefence_session")
    assert session

    assert client.post("/api/logout", json={}).status_code == 200
    assert client.get("/api/state").status_code == 401

    copied = TestClient(client.app)
    copied.cookies.set("timefence_session", session, path="/")
    assert copied.get("/api/state").status_code == 401
    copied.close()


def test_an_oversized_login_body_is_rejected(ready):
    client = ready["client"]
    response = client.post("/api/login", json={"username": "admin", "password": "x" * (17 * 1024)})
    assert response.status_code == 413
    assert client.get("/api/state").status_code == 200


def test_large_service_icons_are_kept(settings):
    from .fakes import FaithfulAdGuard

    app = create_app(settings, adguard=FaithfulAdGuard())
    with TestClient(app) as client:
        complete_setup(client)
        services = {row["id"]: row for row in client.get("/api/services").json()["services"]}

    assert len(services["peacock_tv"]["icon_svg"]) == 9912
    assert len(services["imgur"]["icon_svg"]) == 8072
    assert services["youtube"]["icon_svg"]


def test_the_group_test_probes_a_representative_service_host(settings):
    from .fakes import FaithfulAdGuard

    app = create_app(settings, adguard=FaithfulAdGuard())
    with TestClient(app) as client:
        complete_setup(client)
        group = client.post(
            "/api/groups",
            json={"name": "YouTube", "services": ["youtube"]},
        ).json()["group"]
        results = client.get(f"/api/groups/{group['id']}/test").json()["results"]

    hosts = [row["host"] for row in results]
    assert "youtube.com" in hosts
    assert len(hosts) == 2


def test_a_rewritten_answer_is_not_reported_as_blocked(settings):
    from .fakes import FaithfulAdGuard

    fake = FaithfulAdGuard()
    fake.reasons = {
        "safesearch.example": "FilteredSafeSearch",
        "rewritten.example": "Rewrite",
        "blocked.example": "FilteredBlackList",
    }
    app = create_app(settings, adguard=fake)
    with TestClient(app) as client:
        complete_setup(client)
        group = client.post(
            "/api/groups",
            json={
                "name": "Mixed",
                "domains_text": "safesearch.example\nrewritten.example\nblocked.example",
            },
        ).json()["group"]
        results = {
            row["host"]: row
            for row in client.get(f"/api/groups/{group['id']}/test").json()["results"]
        }

    assert results["safesearch.example"]["blocked"] is False
    assert results["rewritten.example"]["blocked"] is False
    assert results["blocked.example"]["blocked"] is True
