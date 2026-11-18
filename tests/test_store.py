"""The store lock: one connection, one lock, no access without it."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from timefence.api import create_app

from .conftest import locked_conn

ADMIN = {"username": "admin", "password": "admin"}
ADMIN2 = {"username": "admin", "password": "correct horse battery"}


def test_db_access_without_store_lock_raises(settings, fake):
    app = create_app(settings, adguard=fake)
    store = app.state.store
    try:
        with pytest.raises(RuntimeError, match="store lock"):
            store.conn.execute("SELECT 1")
        with store as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
            cursor = conn.execute("SELECT 1 UNION ALL SELECT 2")
            assert cursor.fetchone()[0] == 1
        with pytest.raises(RuntimeError, match="store lock"):
            next(cursor)
        with pytest.raises(RuntimeError, match="store lock"):
            store.conn.commit()
    finally:
        store.close()


def test_reconcile_loop_runs_under_the_guard(settings, fake):
    app = create_app(settings, adguard=fake)
    with TestClient(app):
        errors: list[BaseException] = []

        def work() -> None:
            try:
                app.state.reconciler.run("loop")
            except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
                errors.append(exc)

        thread = threading.Thread(target=work, name="reconcile-proof")
        thread.start()
        thread.join(20)
        assert not thread.is_alive()
        assert errors == []
        assert app.state.reconciler.last_result["reason"] == "loop"


def test_reconciler_refuses_to_run_while_holding_store_lock(settings, fake):
    app = create_app(settings, adguard=fake)
    store = app.state.store
    try:
        with store, pytest.raises(RuntimeError, match="while holding the store lock"):
            app.state.reconciler.run("test")
    finally:
        store.close()


def test_failed_group_update_rolls_back(settings, fake):
    app = create_app(settings, adguard=fake)
    with TestClient(app, raise_server_exceptions=False) as client:
        login = client.post("/api/login", json=ADMIN)
        assert login.status_code == 200, login.text
        client.headers.update({"X-CSRF-Token": login.json()["csrf"]})
        assert (
            client.post("/api/setup", json={"admin_password": ADMIN2["password"]}).status_code
            == 200
        )
        login = client.post("/api/login", json=ADMIN2)
        client.headers.update({"X-CSRF-Token": login.json()["csrf"]})
        created = client.post(
            "/api/groups",
            json={
                "name": "Games",
                "weekday_minutes": 45,
                "services": ["roblox"],
                "domains_text": "kept.example",
            },
        )
        assert created.status_code == 200, created.text
        group = created.json()["group"]

        failed = client.put(
            f"/api/groups/{group['id']}",
            json={
                "name": "Games",
                "weekday_minutes": 45,
                "services": ["youtube", "youtube"],
                "domains_text": "kept.example",
            },
        )
        assert failed.status_code == 500
        with locked_conn(client.app) as conn:
            conn.commit()  # a later write must not persist half of the failed update

        rows = client.get("/api/state").json()["groups"]
        after = [row for row in rows if row["name"] == "Games"][0]
        assert after["services"] == ["roblox"]
        assert after["domains"] == ["kept.example"]


def test_no_route_handler_is_a_coroutine(settings, fake):
    import inspect

    from fastapi.routing import APIRoute

    app = create_app(settings, adguard=fake)
    store = app.state.store
    try:
        routes = [route for route in app.routes if isinstance(route, APIRoute)]
        assert routes
        for route in routes:
            assert not inspect.iscoroutinefunction(route.endpoint), route.path
    finally:
        store.close()


def test_no_store_use_in_coroutines():
    import ast
    from pathlib import Path

    from timefence import api

    source = Path(api.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[int] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.Lambda):
                continue  # plain functions run in the threadpool; they may take the lock
            if isinstance(child, ast.With | ast.AsyncWith):
                for item in child.items:
                    expression = item.context_expr
                    if isinstance(expression, ast.Name) and expression.id == "store":
                        offenders.append(child.lineno)
            if isinstance(child, ast.Name) and child.id == "conn":
                offenders.append(child.lineno)
            walk(child)

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            walk(node)
    assert offenders == []
