"""FastAPI application: HTTP layer for TimeFence."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import APP_TITLE, __version__, alerts, budget, credentials, db, i18n, security
from .adguard import AdGuardClient, AdGuardError
from .config import Settings
from .discover import PublicSuffixList, SuffixList, aggregate, humanize_age, recent_clients
from .reconciler import BLOCKED_REASONS, Reconciler, rule_host

log = logging.getLogger("timefence.web")

DATA = Path(__file__).parent / "data"

# How many sign-ins may run a key derivation at the same time.
LOGIN_SLOTS = threading.BoundedSemaphore(4)
MAX_JSON_BYTES = 1024 * 1024  # any JSON body beyond this is refused before parsing
LOGIN_MAX_BYTES = 16 * 1024  # sign-in is anonymous, so its body is capped hard

USAGE_DAYS = ("7", "14", "30")

# What a user (kid) account may see; the admin payload is a superset.
USER_APP_KEYS = (
    "name",
    "version",
    "role",
    "now",
    "timezone",
    "username",
    "csrf",
    "setup_complete",
)
USER_GROUP_KEYS = (
    "id",
    "name",
    "emoji",
    "minutes_today",
    "bonus_minutes",
    "used_minutes",
    "remaining_minutes",
    "permitted",
    "status",
    "blocked_until",
    "free_until",
    "bedtime_active",
    "bedtime_until",
)
STATIC = Path(__file__).parent / "static"
ICON_CHARS = re.compile(r"^[A-Za-z0-9+/=]+$")
ICON_MAX_CHARS = 32768  # the largest real icon is ~10k chars, never a document


def safe_icon(value: object) -> str:
    """An AdGuard service icon, or "" unless it is plain base64.

    The value goes into a data: URL in the page; anything that is not base64
    could break out of the attribute, so it is dropped rather than escaped.
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > ICON_MAX_CHARS or not ICON_CHARS.match(text):
        return ""
    return text


TEMPLATES = Path(__file__).parent / "templates"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def json_error(status: int, message: str, code: str = "error", **extra: Any) -> JSONResponse:
    return JSONResponse({"error": message, "code": code, **extra}, status_code=status)


def client_ip(request: Request, settings: Settings) -> str:
    return security.client_ip(
        request.client.host if request.client else None,
        ",".join(request.headers.getlist("x-forwarded-for")),
        settings.trusted_proxies,
    )


validate_password = credentials.validate_admin_password


def sets_removed_mode(data: dict) -> bool:
    """True when a payload asks for a mode other than the only one left."""
    mode = data.get("mode")
    return mode is not None and str(mode).strip().lower() != "auto"


# --------------------------------------------------------------------------- #
# application factory
# --------------------------------------------------------------------------- #


def create_app(
    settings: Settings | None = None,
    adguard: AdGuardClient | None = None,
) -> FastAPI:
    os.umask(0o077)  # the database, WAL files and the session secret are private
    settings = settings or Settings.from_env()
    settings.validate()
    logging.getLogger("timefence").setLevel(settings.log_level)

    base = "" if settings.base_path == "/" else settings.base_path

    store = db.Store(settings.db_path)
    with store as conn:
        db.migrate(conn, settings.zone)
        setup_done = db.get_bool_setting(conn, "setup_complete", False)

    session_manager = security.SessionManager(security.load_or_create_secret(settings.data_dir))
    limiter = security.LoginRateLimiter()
    dummy_hash = security.hash_secret(secrets.token_urlsafe(32))
    agh = adguard or AdGuardClient(
        settings.adguard_url, settings.adguard_user, settings.adguard_password
    )
    reconciler = Reconciler(store, agh, settings.zone)

    psl = PublicSuffixList()
    noise = SuffixList.from_file(DATA / "noise_domains.txt")
    critical_patterns = budget.load_critical_patterns(DATA / "critical_domains.txt")

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html"]),
    )

    tz_labels = {"env": "TZ", "system": "system", "default": "default"}
    log.info(
        "Timezone: %s (from %s)",
        settings.tz,
        tz_labels.get(settings.tz_source, settings.tz_source),
    )
    if settings.tz_source == "default":
        log.warning("no timezone is configured; budgets reset at UTC midnight")

    services_cache: dict[str, Any] = {"ts": 0.0, "data": []}
    querylog_cache: dict[str, Any] = {"ts": 0.0, "data": [], "minutes": 0}

    # ---------------------------------------------------------------- setup
    def seed_users() -> None:
        with store as conn:
            count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count:
            return
        # hashing is slow, so it happens outside the store lock
        hashed = security.hash_secret(credentials.DEFAULT_ADMIN_PASSWORD)
        now = int(time.time())
        with store as conn:
            conn.execute(
                "INSERT INTO users(username, role, hash, created_at, updated_at) "
                "VALUES('admin', 'admin', ?, ?, ?)",
                (hashed, now, now),
            )
            db.set_bool_setting(conn, "setup_complete", False)
            conn.commit()

    def read_setup_flag() -> bool:
        with store as conn:
            return db.get_bool_setting(conn, "setup_complete", False)

    def heal_default_password() -> None:
        """Re-open first-run setup once per schema version if an admin is unsafe."""
        with store as conn:
            if db.get_setting(conn, "default_pw_checked") == str(db.SCHEMA_VERSION):
                return
            admins = [
                row
                for row in conn.execute(
                    "SELECT id, username, hash FROM users WHERE role='admin' AND disabled=0"
                )
            ]
            enabled = db.get_bool_setting(conn, "setup_complete", False)
        unsafe = [
            row
            for row in admins
            if security.verify_secret(row["hash"], credentials.DEFAULT_ADMIN_PASSWORD)
        ]
        with store as conn:
            if unsafe and enabled:
                db.set_bool_setting(conn, "setup_complete", False)
                app.state.setup_done = False
                budget.log_event(
                    conn, "setup_reopened", {"usernames": [row["username"] for row in unsafe]}
                )
            db.set_setting(conn, "default_pw_checked", str(db.SCHEMA_VERSION))
            conn.commit()
        if unsafe and enabled:
            log.warning("an admin still uses the default password; first-run setup is required")

    def users_count(role: str | None = None) -> int:
        with store as conn:
            if role:
                return conn.execute(
                    "SELECT COUNT(*) FROM users WHERE role=? AND disabled=0", (role,)
                ).fetchone()[0]
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def user_row(user_id: int):
        with store as conn:
            return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    def setup_complete() -> bool:
        return bool(app.state.setup_done)

    def current_session(request: Request) -> security.SessionData | None:
        return getattr(request.state, "session", None)

    def session_user(sess: security.SessionData | None):
        if not sess or not sess.username:
            return None
        with store as conn:
            return conn.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (sess.username,)
            ).fetchone()

    def user_names() -> dict[int, str]:
        with store as conn:
            return {
                row["id"]: row["username"] for row in conn.execute("SELECT id, username FROM users")
            }

    def visible_group_ids(sess: security.SessionData) -> set[int] | None:
        """Admins see everything; a user sees their own groups plus shared ones."""
        if sess.role == "admin":
            return None
        row = session_user(sess)
        if not row:
            return set()
        with store as conn:
            return {
                group.id
                for group in budget.list_groups(conn)
                if group.owner_id is None or group.owner_id == row["id"]
            }

    def can_see(sess: security.SessionData, group_id: int) -> bool:
        ids = visible_group_ids(sess)
        return ids is None or group_id in ids

    def require(role: str, request: Request) -> security.SessionData:
        sess = current_session(request)
        if not sess:
            raise HTTPException(status_code=401, detail="login required")
        if role == "admin" and sess.role != "admin":
            raise HTTPException(status_code=403, detail="admin required")
        return sess

    def guard_forbidden(request: Request) -> JSONResponse | None:
        """Pre-setup: every API call except login, setup and logout is blocked."""
        if setup_complete():
            return None
        path = request.url.path
        allowed = {
            f"{base}/api/login",
            f"{base}/api/logout",
            f"{base}/api/setup",
            f"{base}/api/meta",
            f"{base}/api/i18n",
        }
        if path in allowed or not path.startswith(f"{base}/api/"):
            return None
        return json_error(403, i18n.t("error.setup_required"), "setup_required")

    # ------------------------------------------------------------ rendering
    def page(name: str, request: Request, **extra: Any) -> HTMLResponse:
        sess = current_session(request)
        template = env.get_template(name)
        html = template.render(
            # "" at the root: "/" would turn every {{ base }}/static/... into
            # a protocol-relative URL (host "static") and break the default install.
            base=base,
            app_title=APP_TITLE,
            app_name=i18n.t("app.title"),
            tagline=i18n.t("app.tagline"),
            unofficial=i18n.t("app.unofficial"),
            version=__version__,
            t=i18n.t,
            csrf=sess.csrf if sess else "",
            role=sess.role if sess else "",
            setup_complete=setup_complete(),
            **extra,
        )
        response = HTMLResponse(html)
        response.headers["Cache-Control"] = "no-store"
        return response

    # -------------------------------------------------------------- context
    def group_payload(group: budget.Group, now: int) -> dict:
        with store as conn:
            payload = budget.group_state(conn, group, settings.zone, now)
        payload["owner"] = user_names().get(group.owner_id) if group.owner_id else None
        return payload

    def reduced_group(payload: dict) -> dict:
        """What a kid account may see about a group: time and state, nothing else."""
        return {key: payload[key] for key in USER_GROUP_KEYS if key in payload}

    def state_payload(role: str, now: int | None = None, group_ids: set[int] | None = None) -> dict:
        now = now or int(time.time())
        with store as conn:
            groups = [
                group_payload(g, now)
                for g in budget.list_groups(conn)
                if group_ids is None or g.id in group_ids
            ]
            history = budget.history(conn, settings.zone, days=7, now=now, group_ids=group_ids)
        payload: dict[str, Any] = {
            "app": {
                "name": i18n.t("app.title"),
                "version": __version__,
                "base_path": settings.base_path,
                "timezone": settings.tz,
                "setup_complete": setup_complete(),
                "role": role,
                "now": now,
            },
            "groups": groups,
            "history": history,
            "adguard_error": reconciler.last_error,
            "adguard_error_kind": reconciler.last_error_kind,
            "adguard_retry_seconds": reconciler.backoff_remaining(),
            "last_sync": int(reconciler.last_ok_ts) if reconciler.last_ok_ts else None,
            "warnings": sorted(
                reconciler.warnings
                | {notice for notice in [migration_notice()] if notice}
                | ({"tz_utc"} if settings.tz_source == "default" else set())
            ),
        }
        return payload

    def migration_notice() -> str | None:
        """A group that needed "Start" now has plain daily minutes: say so once."""
        with store as conn:
            row = conn.execute(
                "SELECT ts, detail FROM events WHERE kind='migration' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        try:
            detail = json.loads(row["detail"] or "{}")
        except (TypeError, ValueError):
            return None
        if int(detail.get("converted_groups") or 0) <= 0:
            return None
        if time.time() - int(row["ts"]) > 7 * 86400:
            return None
        return "session_groups_converted"

    def critical_hits(domains: list[str]) -> list[dict]:
        hits = []
        for domain in domains:
            pattern = budget.critical_match(domain, critical_patterns)
            if pattern:
                hits.append(
                    {"domain": domain, "pattern": pattern, "note": i18n.critical_note(pattern)}
                )
        return hits

    def services_map() -> dict[str, dict]:
        now = time.time()
        if now - services_cache["ts"] > 300 or not services_cache["data"]:
            services_cache["data"] = agh.all_services()
            services_cache["ts"] = now
        return {s["id"]: s for s in services_cache["data"]}

    def managed_domains(exclude_group: int | None = None) -> set[str]:
        """Domains that are already covered, directly or through a service."""
        out: set[str] = set()
        with store as conn:
            groups = [g for g in budget.list_groups(conn) if g.id != exclude_group]
        for group in groups:
            out.update(group.domains)
        if not any(g.services for g in groups):
            return out
        try:
            services = services_map()
        except AdGuardError:
            return out
        for group in groups:
            for service_id in group.services:
                for rule in services.get(service_id, {}).get("rules") or []:
                    host = rule_host(rule)
                    if host:
                        out.add(host)
        return out

    def body(request_body: Any) -> dict:
        return request_body if isinstance(request_body, dict) else {}

    async def json_body(request: Request) -> dict:
        """The request body as a dict, or {} when it is missing, too big or not JSON."""
        buffer = bytearray()
        async for chunk in request.stream():
            buffer.extend(chunk)
            if len(buffer) > MAX_JSON_BYTES:
                return {}
        try:
            data = json.loads(bytes(buffer))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def reconcile(reason: str) -> None:
        """Ask the reconciler to converge; a busy loop is retried by the loop."""
        reconciler.run(reason, wait=5)

    def set_session_cookie(response: JSONResponse, token: str) -> None:
        response.set_cookie(
            session_manager.COOKIE,
            token,
            max_age=settings.session_max_age,
            path=settings.base_path,
            httponly=True,
            samesite="lax",
            secure=settings.secure_cookies,
        )

    def reissue_session(response: JSONResponse, sess: security.SessionData, epoch: int) -> None:
        """Keep the caller signed in after their own account changed."""
        token, _ = session_manager.issue(sess.role, sess.username, sess.uid, epoch, csrf=sess.csrf)
        set_session_cookie(response, token)

    def session_is_valid(sess: security.SessionData) -> bool:
        """A cookie is only good while its account, role, name and epoch agree."""
        with store as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (sess.uid,)).fetchone()
        if not row or row["disabled"]:
            return False
        return (
            row["role"] == sess.role
            and str(row["username"]).lower() == sess.username.lower()
            and int(row["session_epoch"]) == sess.epoch
        )

    # ------------------------------------------------------------- lifespan
    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        seed_users()
        heal_default_password()
        ok, detail = agh.test_connection()
        if ok:
            log.info("AdGuard Home connection OK: %s", detail)
        else:
            log.error("AdGuard Home connection FAILED: %s", detail)
        log.info("configuration: %s", settings.redacted())
        app.state.setup_done = read_setup_flag()
        worker = alerts.AlertWorker(store, settings)
        app.state.alerts = worker
        reconciler.alert_worker = worker
        worker.start()
        reconciler.run("startup")
        stop = asyncio.Event()

        async def loop() -> None:
            while not stop.is_set():
                try:
                    await asyncio.to_thread(reconciler.run, "loop")
                except Exception:  # pragma: no cover - defensive
                    log.exception("reconciler crashed")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=settings.reconcile_interval)
                except TimeoutError:
                    continue

        task = asyncio.create_task(loop(), name="timefence-reconciler")
        try:
            yield
        finally:
            # Shutdown: stop the loop, wait for it, then block every managed
            # group. A later run() returns immediately; the write below does not.
            reconciler.stopping.set()
            agh.timeout = min(float(getattr(agh, "timeout", 10.0)), 3.0)
            stop.set()
            done, _ = await asyncio.wait({task}, timeout=8)
            if task not in done:
                log.warning("reconciler loop did not stop within 8 s; continuing shutdown")
            # The alert worker must be drained and joined before the store
            # closes: its bookkeeping writes go through the same lock.
            reconciler.alert_worker = None
            worker.stop.set()
            worker.wake()
            worker.join(2.5)
            if worker.is_alive():
                log.warning("alert worker did not stop within 2.5 s; continuing shutdown")
            app.state.alerts = None
            await asyncio.to_thread(reconciler.block_all)
            store.close()

    app = FastAPI(
        title=APP_TITLE,
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.limiter = limiter
    app.state.store = store
    app.state.conn = store.conn
    app.state.setup_done = setup_done
    app.state.alerts = None  # the lifespan starts the worker; create_app does not
    app.state.adguard = agh
    app.state.reconciler = reconciler

    # ------------------------------------------------------------ middleware
    @app.middleware("http")
    async def middleware(request: Request, call_next):
        request.state.session = session_manager.read(request.cookies.get(session_manager.COOKIE))
        path = request.url.path
        stale_cookie = False
        if (
            request.state.session is not None
            and not path.startswith(f"{base}/static/")
            and path != f"{base}/healthz"
        ):
            approved = await asyncio.to_thread(session_is_valid, request.state.session)
            if not approved:
                request.state.session = None
                stale_cookie = True
        api_prefix = f"{base}/api/"
        mutating = request.method in {"POST", "PUT", "PATCH", "DELETE"} and path.startswith(
            api_prefix
        )
        if path == f"{base}/api/login" and request.method == "POST":
            declared = request.headers.get("content-length")
            try:
                too_big = declared is not None and int(declared) > LOGIN_MAX_BYTES
            except ValueError:
                too_big = True
            if too_big:
                return json_error(413, i18n.t("error.body_too_large"), "body_too_large")
        if mutating and path not in {f"{base}/api/login"}:
            sess = request.state.session
            token = request.headers.get("x-csrf-token", "")
            if not sess or not token or not _constant_eq(token, sess.csrf):
                return json_error(403, "CSRF token missing or invalid", "csrf")
        if mutating and path not in {f"{base}/api/login", f"{base}/api/logout"}:
            sess = request.state.session
            if sess and sess.role != "admin":
                return json_error(403, i18n.t("error.read_only"), "read_only")
        blocked = guard_forbidden(request)
        if blocked is not None:
            return blocked
        response = await call_next(request)
        if stale_cookie:
            response.delete_cookie(session_manager.COOKIE, path=settings.base_path)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
            "form-action 'self'",
        )
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        return response

    # ----------------------------------------------------------------- pages
    index_path = base + "/"

    @app.get(index_path, response_class=HTMLResponse)
    def index(request: Request):
        sess = current_session(request)
        if not sess:
            return RedirectResponse(f"{base}/login")
        if sess.role != "admin":
            return page("kid.html", request)
        if not setup_complete():
            return RedirectResponse(f"{base}/setup")
        return page("app.html", request)

    if base:

        @app.get(base, response_class=HTMLResponse)
        def index_noslash():
            return RedirectResponse(index_path)

    @app.get(f"{base}/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if current_session(request):
            return RedirectResponse(base + "/")
        return page("login.html", request)

    @app.get(f"{base}/setup", response_class=HTMLResponse)
    def setup_page(request: Request):
        sess = current_session(request)
        if not sess:
            return RedirectResponse(f"{base}/login")
        if sess.role != "admin":
            return RedirectResponse(base + "/")
        if setup_complete():
            return RedirectResponse(base + "/")
        return page("setup.html", request)

    # ------------------------------------------------------------------ auth
    @app.post(f"{base}/api/login")
    def api_login(request: Request, data: dict = Depends(json_body)):
        if (
            request.headers.get("content-type", "").split(";")[0].strip().lower()
            != "application/json"
        ):
            return json_error(415, i18n.t("login.json_required"), "unsupported_media_type")
        # Sign-in runs a key derivation: bound how many run at once so a
        # burst cannot exhaust the box.
        if not LOGIN_SLOTS.acquire(blocking=False):
            return json_error(429, i18n.t("login.busy"), "busy")
        try:
            ip = client_ip(request, settings)
            ip_key = f"ip:{ip}"
            locked = limiter.locked_for(ip_key)
            if locked:  # locked addresses do no work at all: no hash, no event
                return json_error(
                    429, i18n.t("login.locked", minutes=max(1, locked // 60 + 1)), "locked"
                )
            username = str(data.get("username", "")).strip()
            password = str(data.get("password", ""))
            if not username or not password:
                return json_error(400, i18n.t("login.missing"), "bad_request")
            if not credentials.USERNAME_RE.match(username):
                limiter.record_failure(ip_key)
                security.verify_secret(dummy_hash, password)
                remaining = limiter.locked_for(ip_key)
                if remaining:
                    return json_error(
                        429, i18n.t("login.locked", minutes=max(1, remaining // 60 + 1)), "locked"
                    )
                return json_error(401, i18n.t("login.wrong"), "bad_credentials")
            pair_key = f"pair:{ip}|{username.lower()}"
            locked = limiter.locked_for(pair_key)
            if locked:
                return json_error(
                    429, i18n.t("login.locked", minutes=max(1, locked // 60 + 1)), "locked"
                )
            with store as conn:
                row = conn.execute(
                    "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
                ).fetchone()
            # hashing is slow: never while holding the store lock
            if row and not row["disabled"]:
                ok = security.verify_secret(row["hash"], password)
            else:
                security.verify_secret(dummy_hash, password)
                ok = False
            if ok and row["role"] != "admin" and not setup_complete():
                return json_error(403, i18n.t("error.setup_required"), "setup_required")
            if not ok:
                limiter.record_failure(pair_key)
                limiter.record_failure(ip_key)
                with store as conn:
                    budget.log_event(conn, "login_failed", {"username": username[:40], "ip": ip})
                remaining = max(limiter.locked_for(pair_key), limiter.locked_for(ip_key))
                if remaining:
                    return json_error(
                        429, i18n.t("login.locked", minutes=max(1, remaining // 60 + 1)), "locked"
                    )
                return json_error(401, i18n.t("login.wrong"), "bad_credentials")
            limiter.reset(pair_key)  # only this pair, never the address
            new_hash = (
                security.hash_secret(password) if security.needs_rehash(row["hash"]) else None
            )
            with store as conn:
                if new_hash:
                    conn.execute(
                        "UPDATE users SET hash=?, updated_at=? WHERE id=?",
                        (new_hash, int(time.time()), row["id"]),
                    )
                conn.execute(
                    "UPDATE users SET last_login=? WHERE id=?", (int(time.time()), row["id"])
                )
                conn.commit()
                budget.log_event(
                    conn, "login", {"username": row["username"], "role": row["role"], "ip": ip}
                )
            token, csrf = session_manager.issue(
                row["role"], row["username"], row["id"], int(row["session_epoch"])
            )
            response = JSONResponse(
                {
                    "ok": True,
                    "role": row["role"],
                    "username": row["username"],
                    "csrf": csrf,
                    "setup_complete": setup_complete(),
                }
            )
            set_session_cookie(response, token)
            return response
        finally:
            LOGIN_SLOTS.release()

    @app.post(f"{base}/api/logout")
    def api_logout(request: Request):
        sess = current_session(request)
        if sess is not None:
            # A copied cookie must stop working: logout signs the account out
            # everywhere instead of only clearing this browser's cookie.
            with store as conn:
                credentials.bump_epoch(conn, sess.uid)
                conn.commit()
        response = JSONResponse({"ok": True})
        response.delete_cookie(session_manager.COOKIE, path=settings.base_path)
        return response

    @app.post(f"{base}/api/setup")
    def api_setup(request: Request, data: dict = Depends(json_body)):
        sess = require("admin", request)
        if setup_complete():
            return json_error(400, "setup already completed", "already_setup")
        admin_password = str(data.get("admin_password", ""))
        problem = validate_password(admin_password)
        if problem:
            return json_error(400, problem, "invalid")
        hashed = security.hash_secret(admin_password)
        with store as conn:
            credentials.store_password(conn, sess.uid, hashed)
            others = [
                row
                for row in conn.execute(
                    "SELECT id, username, hash FROM users "
                    "WHERE role='admin' AND disabled=0 AND id IS NOT ?",
                    (sess.uid,),
                )
            ]
        # other admins still on the default password are a first-run risk
        unsafe = [
            row
            for row in others
            if security.verify_secret(row["hash"], credentials.DEFAULT_ADMIN_PASSWORD)
        ]
        disabled_accounts: list[str] = []
        with store as conn:
            for row in unsafe:
                conn.execute(
                    "UPDATE users SET disabled=1, updated_at=? WHERE id=?",
                    (int(time.time()), row["id"]),
                )
                credentials.bump_epoch(conn, row["id"])
                budget.log_event(
                    conn,
                    "default_password_account_disabled",
                    {"username": row["username"], "actor": sess.username},
                )
                disabled_accounts.append(row["username"])
            db.set_bool_setting(conn, "setup_complete", True)
            budget.log_event(conn, "setup_completed", {"username": sess.username})
            conn.commit()
            epoch = int(
                conn.execute("SELECT session_epoch FROM users WHERE id=?", (sess.uid,)).fetchone()[
                    0
                ]
            )
        app.state.setup_done = True
        log.info("first-run setup completed")
        response = JSONResponse({"ok": True, "disabled_accounts": disabled_accounts})
        reissue_session(response, sess, epoch)
        return response

    @app.get(f"{base}/api/meta")
    def api_meta(request: Request):
        sess = current_session(request)
        return {
            "app": i18n.t("app.title"),
            "version": __version__,
            "tagline": i18n.t("app.tagline"),
            "unofficial": i18n.t("app.unofficial"),
            "base_path": settings.base_path,
            "timezone": settings.tz,
            "setup_complete": setup_complete(),
            "role": sess.role if sess else None,
            "now": int(time.time()),
        }

    @app.get(f"{base}/api/i18n")
    def api_i18n():
        return i18n.strings()

    # ----------------------------------------------------------------- state
    @app.get(f"{base}/api/state")
    def api_state(request: Request):
        sess = require("user", request)
        payload = state_payload(sess.role, group_ids=visible_group_ids(sess))
        payload["app"]["csrf"] = sess.csrf
        payload["app"]["username"] = sess.username
        if sess.role != "admin":
            return {
                "app": {key: payload["app"][key] for key in USER_APP_KEYS if key in payload["app"]},
                "groups": [reduced_group(group) for group in payload["groups"]],
            }
        return payload

    # ---------------------------------------------------------------- groups
    @app.get(f"{base}/api/groups")
    def api_groups(request: Request):
        sess = require("user", request)
        now = int(time.time())
        ids = visible_group_ids(sess)
        with store as conn:
            groups = [
                group_payload(g, now)
                for g in budget.list_groups(conn)
                if ids is None or g.id in ids
            ]
        if sess.role != "admin":
            groups = [reduced_group(group) for group in groups]
        return {"groups": groups}

    def group_payload_problem(data: dict) -> JSONResponse | None:
        """Services and domains are lists of strings; anything else is a 400."""
        for field in ("services", "domains"):
            value = data.get(field)
            if value is not None and not isinstance(value, list):
                return json_error(400, i18n.t("error.group_fields"), "invalid")
        return None

    def group_from_payload(data: dict, existing: budget.Group | None = None) -> dict:
        def int_or(value: Any, fallback: int) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return fallback

        weekday = int_or(data.get("weekday_minutes"), existing.weekday_minutes if existing else 60)
        weekend_raw = data.get("weekend_minutes")
        weekend = weekday if weekend_raw in (None, "") else int_or(weekend_raw, weekday)
        domains_text = data.get("domains_text")
        if domains_text is None:
            domains = data.get("domains") or (existing.domains if existing else [])
        else:
            domains = budget.normalize_domains(str(domains_text))
        owner_raw = data.get("owner_id", existing.owner_id if existing else None)
        if owner_raw in ("", None):
            owner_id = None
        else:
            try:
                owner_id = int(owner_raw)
            except (TypeError, ValueError):
                owner_id = None
        if owner_id is not None:
            with store as conn:
                if not conn.execute("SELECT 1 FROM users WHERE id=?", (owner_id,)).fetchone():
                    raise ValueError("owner not found")
        return {
            "name": str(data.get("name", existing.name if existing else "")).strip(),
            "emoji": str(data.get("emoji", existing.emoji if existing else "")).strip(),
            "weekday_minutes": weekday,
            "weekend_minutes": weekend,
            "owner_id": owner_id,
            "set_owner": "owner_id" in data,
            "services": [
                str(s).strip().lower() for s in (data.get("services") or []) if str(s).strip()
            ],
            "domains": domains,
            "windows": data.get("windows"),
            "paused": bool(data.get("paused", existing.paused if existing else False)),
        }

    @app.post(f"{base}/api/groups")
    def api_group_create(request: Request, data: dict = Depends(json_body)):
        sess = require("admin", request)
        if sets_removed_mode(data):
            return json_error(400, i18n.t("error.mode_removed"), "mode_removed")
        problem = group_payload_problem(data)
        if problem is not None:
            return problem
        fields = group_from_payload(data)
        hits = critical_hits(fields["domains"])
        if hits and not data.get("confirm_critical"):
            return json_error(
                409,
                i18n.t("groups.critical_title"),
                "critical_domains",
                critical=hits,
            )
        fields.pop("set_owner", None)
        with store as conn:
            try:
                group = budget.create_group(conn, **fields)
            except budget.WindowError:
                return json_error(400, i18n.t("error.windows_invalid"), "invalid_windows")
            except budget.ValidationError as exc:
                return json_error(400, str(exc), "invalid")
            budget.log_event(conn, "group_create", {"group": group.name, "actor": sess.role})
        reconcile("group_create")
        return {"ok": True, "group": group_payload(group, int(time.time()))}

    @app.put(f"{base}/api/groups/{{group_id}}")
    def api_group_update(group_id: int, request: Request, data: dict = Depends(json_body)):
        sess = require("admin", request)
        if sets_removed_mode(data):
            return json_error(400, i18n.t("error.mode_removed"), "mode_removed")
        problem = group_payload_problem(data)
        if problem is not None:
            return problem
        with store as conn:
            existing = budget.get_group(conn, group_id)
            if not existing or existing.deleted:
                return json_error(404, i18n.t("error.not_found"), "not_found")
            fields = group_from_payload(data, existing)
            hits = critical_hits(fields["domains"])
            if hits and not data.get("confirm_critical"):
                return json_error(
                    409, i18n.t("groups.critical_title"), "critical_domains", critical=hits
                )
            try:
                group = budget.update_group(conn, group_id, **fields)
            except budget.WindowError:
                return json_error(400, i18n.t("error.windows_invalid"), "invalid_windows")
            except budget.ValidationError as exc:
                return json_error(400, str(exc), "invalid")
            budget.log_event(conn, "group_update", {"group": group.name, "actor": sess.role})
        reconcile("group_update")
        return {"ok": True, "group": group_payload(group, int(time.time()))}

    @app.delete(f"{base}/api/groups/{{group_id}}")
    def api_group_delete(group_id: int, request: Request):
        sess = require("admin", request)
        with store as conn:
            group = budget.get_group(conn, group_id)
            if not group or group.deleted:
                return json_error(404, i18n.t("error.not_found"), "not_found")
            name = budget.delete_group(conn, group_id)
            budget.log_event(conn, "group_delete", {"group": name, "actor": sess.role})
        reconcile("group_delete")
        return {"ok": True, "deleted": name}

    @app.post(f"{base}/api/groups/{{group_id}}/pause")
    def api_group_pause(group_id: int, request: Request, data: dict = Depends(json_body)):
        sess = require("admin", request)
        paused = bool(data.get("paused", True))
        with store as conn:
            try:
                group = budget.update_group(conn, group_id, paused=paused)
            except budget.ValidationError as exc:
                return json_error(400, str(exc), "invalid")
            budget.log_event(
                conn, "group_pause", {"group": group.name, "paused": paused, "actor": sess.role}
            )
        reconcile("group_pause")
        return {"ok": True, "group": group_payload(group, int(time.time()))}

    @app.post(f"{base}/api/groups/{{group_id}}/bonus")
    def api_group_bonus(group_id: int, request: Request, data: dict = Depends(json_body)):
        sess = require("admin", request)
        try:
            minutes = int(data.get("minutes", 15))
        except (TypeError, ValueError):
            return json_error(400, "minutes is required", "bad_request")
        with store as conn:
            group = budget.get_group(conn, group_id)
            if not group or group.deleted:
                return json_error(404, i18n.t("error.not_found"), "not_found")
            try:
                budget.add_bonus(conn, group_id, minutes, settings.zone)
            except budget.ValidationError as exc:
                return json_error(400, str(exc), "invalid")
            budget.log_event(
                conn, "admin_bonus", {"group": group.name, "minutes": minutes, "actor": sess.role}
            )
        reconcile("admin_bonus")
        return {"ok": True, "group": group_payload(group, int(time.time()))}

    @app.post(f"{base}/api/groups/{{group_id}}/free")
    def api_group_free(group_id: int, request: Request, data: dict = Depends(json_body)):
        sess = require("admin", request)
        with store as conn:
            group = budget.get_group(conn, group_id)
            if not group or group.deleted:
                return json_error(404, i18n.t("error.not_found"), "not_found")
            try:
                until = budget.parse_hhmm(
                    str(data.get("until", "")), int(time.time()), settings.zone
                )
            except budget.ValidationError as exc:
                return json_error(400, str(exc), "invalid")
            budget.set_free_until(conn, group_id, until)
            budget.log_event(
                conn,
                "admin_free",
                {"group": group.name, "until": until, "actor": sess.role},
            )
        reconcile("admin_free")
        return {"ok": True, "until": until, "group": group_payload(group, int(time.time()))}

    @app.post(f"{base}/api/groups/{{group_id}}/free/clear")
    def api_group_free_clear(group_id: int, request: Request):
        sess = require("admin", request)
        with store as conn:
            group = budget.get_group(conn, group_id)
            if not group or group.deleted:
                return json_error(404, i18n.t("error.not_found"), "not_found")
            budget.clear_free(conn, group_id)
            budget.log_event(conn, "admin_free_clear", {"group": group.name, "actor": sess.role})
        reconcile("admin_free_clear")
        return {"ok": True, "group": group_payload(group, int(time.time()))}

    @app.post(f"{base}/api/groups/{{group_id}}/block")
    def api_group_block(group_id: int, request: Request):
        sess = require("admin", request)
        with store as conn:
            group = budget.get_group(conn, group_id)
            if not group or group.deleted:
                return json_error(404, i18n.t("error.not_found"), "not_found")
            budget.clear_free(conn, group_id)
            budget.set_force_block(
                conn, group_id, budget.next_local_midnight(int(time.time()), settings.zone)
            )
            budget.log_event(conn, "admin_block", {"group": group.name, "actor": sess.role})
        reconcile("admin_block")
        return {
            "ok": True,
            "group": group_payload(group, int(time.time())),
        }

    # ------------------------------------------------------------------ test
    def _probe_hosts(service_id: str, hosts: list[str]) -> list[str]:
        """The two hosts that best represent a service for a one-shot probe.

        The real catalogue sorts its rules alphabetically, so the first two are
        often a CDN (`ggpht.cn`) rather than the service itself. Prefer the
        service's own `.com`, then any host containing its id, then the shortest
        (the most general) name.
        """
        wanted = service_id.replace("_", "").replace("-", "")

        def rank(host: str) -> tuple:
            return (
                0 if host == f"{wanted}.com" else 1,
                0 if wanted in host.replace("-", "").replace(".", "") else 1,
                len(host.split(".")),
                len(host),
                host,
            )

        return sorted(hosts, key=rank)[:2]

    @app.get(f"{base}/api/groups/{{group_id}}/test")
    def api_group_test(group_id: int, request: Request):
        require("admin", request)
        with store as conn:
            group = budget.get_group(conn, group_id)
        if not group or group.deleted:
            return json_error(404, i18n.t("error.not_found"), "not_found")
        hostnames = list(group.domains)
        if group.services:
            try:
                services = services_map()
            except AdGuardError as exc:
                return json_error(502, i18n.t("error.adguard", detail=str(exc)), "adguard")
            for service_id in group.services:
                hosts = [
                    host
                    for host in (
                        rule_host(rule)
                        for rule in (services.get(service_id, {}).get("rules") or [])
                    )
                    if host
                ]
                hostnames.extend(_probe_hosts(service_id, hosts))
        results = []
        for host in dict.fromkeys(hostnames):
            try:
                info = agh.check_host(host)
            except AdGuardError as exc:
                return json_error(502, i18n.t("error.adguard", detail=str(exc)), "adguard")
            reason = info.get("reason", "")
            results.append(
                {
                    "host": host,
                    # one definition, shared with metering: a rewrite is not a block
                    "blocked": reason in BLOCKED_REASONS,
                    "reason": reason,
                }
            )
        return {"ok": True, "results": results}

    # -------------------------------------------------------------- services
    @app.get(f"{base}/api/services")
    def api_services(request: Request):
        require("admin", request)
        try:
            services_map()
        except AdGuardError as exc:
            return json_error(502, i18n.t("error.adguard", detail=str(exc)), "adguard")
        with store as conn:
            used = {s: g.name for g in budget.list_groups(conn) for s in g.services}
        out = [
            {
                "id": s["id"],
                "name": s.get("name", s["id"]),
                "icon_svg": safe_icon(s.get("icon_svg")),
                "group": used.get(s["id"]),
            }
            for s in services_cache["data"]
        ]
        return {"services": out}

    # -------------------------------------------------------------- discover
    @app.get(f"{base}/api/discover")
    def api_discover(
        request: Request,
        minutes: int = 15,
        client: str = "",
        show_all: bool = False,
    ):
        require("admin", request)
        minutes = minutes if minutes in {5, 15, 30} else 15
        now = int(time.time())
        if now - querylog_cache["ts"] > 20 or querylog_cache["minutes"] < minutes:
            try:
                querylog_cache["data"], _ = agh.querylog_since(now - minutes * 60)
            except AdGuardError as exc:
                return json_error(502, i18n.t("error.adguard", detail=str(exc)), "adguard")
            querylog_cache["ts"] = now
            querylog_cache["minutes"] = minutes
        entries = querylog_cache["data"]
        domains = aggregate(
            entries,
            window_minutes=minutes,
            psl=psl,
            noise=noise,
            managed_domains=managed_domains(),
            now=now,
            client=client or None,
            show_all=show_all,
        )
        for item in domains:
            item["last_seen_ago"] = humanize_age(now - item["last_seen"])
        return {
            "ok": True,
            "minutes": minutes,
            "domains": domains,
            "clients": recent_clients(entries, now),
            "now": now,
        }

    @app.post(f"{base}/api/discover/add")
    def api_discover_add(request: Request, data: dict = Depends(json_body)):
        sess = require("admin", request)
        try:
            group_id = int(data.get("group_id"))
        except (TypeError, ValueError):
            return json_error(400, "group_id is required", "bad_request")
        raw = data.get("domains") or []
        if not isinstance(raw, list):
            return json_error(400, "domains must be a list", "bad_request")
        try:
            domains = list(dict.fromkeys(budget.normalize_domain(str(d)) for d in raw))
        except budget.ValidationError as exc:
            return json_error(400, str(exc), "invalid")
        hits = critical_hits(domains)
        if hits and not data.get("confirm_critical"):
            return json_error(
                409, i18n.t("groups.critical_title"), "critical_domains", critical=hits
            )
        with store as conn:
            group = budget.get_group(conn, group_id)
            if not group or group.deleted:
                return json_error(404, i18n.t("error.not_found"), "not_found")
            merged = list(dict.fromkeys(group.domains + domains))
            try:
                group = budget.update_group(conn, group_id, domains=merged)
            except budget.ValidationError as exc:
                return json_error(400, str(exc), "invalid")
            budget.log_event(
                conn,
                "discover_add",
                {"group": group.name, "domains": domains, "actor": sess.role},
            )
        reconcile("discover_add")
        return {"ok": True, "group": group_payload(group, int(time.time())), "added": domains}

    # ----------------------------------------------------------------- usage
    def usage_rows(days: str) -> tuple[list[dict], JSONResponse | None]:
        if days not in USAGE_DAYS:
            return [], json_error(400, i18n.t("error.usage_days"), "invalid")
        now = int(time.time())
        with store as conn:
            return budget.usage_range(conn, settings.zone, int(days), now), None

    @app.get(f"{base}/api/usage")
    def api_usage(request: Request, days: str = "7"):
        require("admin", request)
        rows, problem = usage_rows(days)
        if problem is not None:
            return problem
        response = JSONResponse({"days": int(days), "rows": rows})
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get(f"{base}/api/usage.csv")
    def api_usage_csv(request: Request, days: str = "7"):
        require("admin", request)
        rows, problem = usage_rows(days)
        if problem is not None:
            return problem
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["group", "date", "used_minutes", "bonus_minutes"])
        for row in rows:
            writer.writerow(
                [csv_cell(row["group"]), row["day"], row["used_minutes"], row["bonus_minutes"]]
            )
        response = Response(
            content=buffer.getvalue().encode("utf-8"),
            media_type="text/csv; charset=utf-8",
        )
        response.headers["Content-Disposition"] = 'attachment; filename="timefence-usage.csv"'
        response.headers["Cache-Control"] = "no-store"
        return response

    # ---------------------------------------------------------------- alerts
    def alerts_payload() -> dict:
        with store as conn:
            return alerts.public_config(conn, int(time.time()))

    @app.get(f"{base}/api/alerts")
    def api_alerts(request: Request):
        require("admin", request)
        response = JSONResponse(alerts_payload())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.put(f"{base}/api/alerts")
    def api_alerts_update(request: Request, data: dict = Depends(json_body)):
        require("admin", request)
        with store as conn:
            try:
                alerts.write_config(conn, data)
            except alerts.TopicError:
                return json_error(400, i18n.t("error.alerts_topic"), "invalid")
            except budget.ValidationError:
                return json_error(400, i18n.t("error.alerts_invalid"), "invalid")
        response = JSONResponse(alerts_payload())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post(f"{base}/api/alerts/test")
    def api_alerts_test(request: Request):
        require("admin", request)
        if not alerts.TEST_LOCK.acquire(blocking=False):
            return json_error(429, i18n.t("alerts.busy"), "busy")
        try:
            category = alerts.send_test(store, settings)
        finally:
            alerts.TEST_LOCK.release()
        if category == "ok":
            return {"ok": True}
        return {"ok": False, "category": category}

    # --------------------------------------------------------------- account
    @app.post(f"{base}/api/account/password")
    def api_account_password(request: Request, data: dict = Depends(json_body)):
        sess = require("admin", request)
        ip = client_ip(request, settings)
        pair_key = f"pair:{ip}|{(sess.username or '').lower()}"
        locked = limiter.locked_for(pair_key)
        if locked:
            return json_error(
                429, i18n.t("login.locked", minutes=max(1, locked // 60 + 1)), "locked"
            )
        current = str(data.get("current_password", ""))
        new = str(data.get("new_password", ""))
        with store as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (sess.uid,)).fetchone()
        if not row or not security.verify_secret(row["hash"], current):
            limiter.record_failure(pair_key)
            return json_error(400, i18n.t("error.wrong_password"), "wrong_password")
        problem = credentials.validate(row["role"], new)
        if problem:
            return json_error(400, problem, "invalid")
        hashed = security.hash_secret(new)
        with store as conn:
            credentials.store_password(conn, sess.uid, hashed)
            conn.commit()
            budget.log_event(
                conn, "password_changed", {"username": sess.username, "actor": sess.username}
            )
            epoch = int(
                conn.execute("SELECT session_epoch FROM users WHERE id=?", (sess.uid,)).fetchone()[
                    0
                ]
            )
        response = JSONResponse({"ok": True})
        reissue_session(response, sess, epoch)
        return response

    # --------------------------------------------------------------- users
    def public_user(row) -> dict:
        return {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "disabled": bool(row["disabled"]),
            "created_at": row["created_at"],
            "last_login": row["last_login"],
        }

    @app.get(f"{base}/api/users")
    def api_users(request: Request):
        require("admin", request)
        with store as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY role DESC, username").fetchall()
        return {"users": [public_user(r) for r in rows]}

    @app.post(f"{base}/api/users")
    def api_user_create(request: Request, data: dict = Depends(json_body)):
        sess = require("admin", request)
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        role = str(data.get("role", "user")).strip().lower()
        problem = credentials.validate_username(username) or credentials.validate(role, password)
        if problem:
            return json_error(400, problem, "invalid")
        with store as conn:
            exists = conn.execute(
                "SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (username,)
            ).fetchone()
        if exists:
            return json_error(409, i18n.t("users.exists"), "exists")
        hashed = security.hash_secret(password)
        now = int(time.time())
        with store as conn:
            cur = conn.execute(
                "INSERT INTO users(username, role, hash, created_at, updated_at) VALUES(?,?,?,?,?)",
                (username, role, hashed, now, now),
            )
            conn.commit()
            budget.log_event(
                conn, "user_create", {"username": username, "role": role, "actor": sess.username}
            )
            row = user_row(int(cur.lastrowid))
        return {"ok": True, "user": public_user(row)}

    @app.post(f"{base}/api/users/{{user_id}}/password")
    def api_user_password(user_id: int, request: Request, data: dict = Depends(json_body)):
        sess = require("admin", request)
        password = str(data.get("password", ""))
        row = user_row(user_id)
        if not row:
            return json_error(404, i18n.t("error.not_found"), "not_found")
        problem = credentials.validate(row["role"], password)
        if problem:
            return json_error(400, problem, "invalid")
        hashed = security.hash_secret(password)
        with store as conn:
            credentials.store_password(conn, user_id, hashed)
            conn.commit()
            budget.log_event(
                conn, "user_password", {"username": row["username"], "actor": sess.username}
            )
            caller_epoch = (
                int(
                    conn.execute(
                        "SELECT session_epoch FROM users WHERE id=?", (sess.uid,)
                    ).fetchone()[0]
                )
                if sess.uid == user_id
                else None
            )
        response = JSONResponse({"ok": True})
        if caller_epoch is not None:
            reissue_session(response, sess, caller_epoch)
        return response

    @app.post(f"{base}/api/users/{{user_id}}/role")
    def api_user_role(user_id: int, request: Request, data: dict = Depends(json_body)):
        sess = require("admin", request)
        role = str(data.get("role", "")).strip().lower()
        if role not in ("admin", "user"):
            return json_error(400, i18n.t("users.bad_role"), "invalid")
        with store as conn:
            row = user_row(user_id)
            if not row:
                return json_error(404, i18n.t("error.not_found"), "not_found")
            if row["role"] == "admin" and role != "admin" and users_count("admin") <= 1:
                return json_error(409, i18n.t("users.last_admin"), "last_admin")
            conn.execute(
                "UPDATE users SET role=?, updated_at=? WHERE id=?",
                (role, int(time.time()), user_id),
            )
            credentials.bump_epoch(conn, user_id)
            conn.commit()
            budget.log_event(
                conn,
                "user_role",
                {"username": row["username"], "role": role, "actor": sess.username},
            )
            caller_epoch = (
                int(
                    conn.execute(
                        "SELECT session_epoch FROM users WHERE id=?", (sess.uid,)
                    ).fetchone()[0]
                )
                if sess.uid == user_id
                else None
            )
        response = JSONResponse({"ok": True, "user": public_user(user_row(user_id))})
        if caller_epoch is not None:
            reissue_session(response, sess, caller_epoch)
        return response

    @app.post(f"{base}/api/users/{{user_id}}/disabled")
    def api_user_disabled(user_id: int, request: Request, data: dict = Depends(json_body)):
        sess = require("admin", request)
        disabled = bool(data.get("disabled", True))
        with store as conn:
            row = user_row(user_id)
            if not row:
                return json_error(404, i18n.t("error.not_found"), "not_found")
            if row["role"] == "admin" and disabled and users_count("admin") <= 1:
                return json_error(409, i18n.t("users.last_admin"), "last_admin")
            conn.execute(
                "UPDATE users SET disabled=?, updated_at=? WHERE id=?",
                (int(disabled), int(time.time()), user_id),
            )
            if disabled:
                credentials.bump_epoch(conn, user_id)
            conn.commit()
            budget.log_event(
                conn,
                "user_disabled",
                {"username": row["username"], "disabled": disabled, "actor": sess.username},
            )
        return {"ok": True, "user": public_user(user_row(user_id))}

    @app.delete(f"{base}/api/users/{{user_id}}")
    def api_user_delete(user_id: int, request: Request):
        sess = require("admin", request)
        with store as conn:
            row = user_row(user_id)
            if not row:
                return json_error(404, i18n.t("error.not_found"), "not_found")
            if row["username"].lower() == (sess.username or "").lower():
                return json_error(409, i18n.t("users.self_delete"), "self")
            if row["role"] == "admin" and users_count("admin") <= 1:
                return json_error(409, i18n.t("users.last_admin"), "last_admin")
            conn.execute("UPDATE groups SET owner_id=NULL WHERE owner_id=?", (user_id,))
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            conn.commit()
            budget.log_event(
                conn, "user_delete", {"username": row["username"], "actor": sess.username}
            )
        return {"ok": True, "deleted": row["username"]}

    @app.post(f"{base}/api/groups/{{group_id}}/reset")
    def api_group_reset(group_id: int, request: Request):
        sess = require("admin", request)
        with store as conn:
            group = budget.get_group(conn, group_id)
            if not group or group.deleted:
                return json_error(404, i18n.t("error.not_found"), "not_found")
            budget.reset_today(conn, group_id, settings.zone)
            budget.clear_force_block(conn, group_id)
            budget.log_event(conn, "admin_reset", {"group": group.name, "actor": sess.username})
        reconcile("admin_reset")
        with store as conn:
            group = budget.get_group(conn, group_id)
        return {"ok": True, "group": group_payload(group, int(time.time()))}

    @app.post(f"{base}/api/groups/{{group_id}}/force/clear")
    def api_group_force_clear(group_id: int, request: Request):
        sess = require("admin", request)
        with store as conn:
            group = budget.get_group(conn, group_id)
            if not group or group.deleted:
                return json_error(404, i18n.t("error.not_found"), "not_found")
            budget.clear_force_block(conn, group_id)
            budget.log_event(conn, "admin_unblock", {"group": group.name, "actor": sess.username})
        reconcile("admin_unblock")
        with store as conn:
            group = budget.get_group(conn, group_id)
        return {"ok": True, "group": group_payload(group, int(time.time()))}

    @app.post(f"{base}/api/adguard/test")
    def api_adguard_test(request: Request):
        require("admin", request)
        ok, detail = reconciler.test_connection()
        if ok:
            reconcile("adguard_test")
        return {"ok": ok, "detail": detail}

    @app.get(f"{base}/healthz")
    def healthz():
        """No auth and no lock: a shallow, honest liveness answer."""
        last = reconciler.last_ok_ts
        stale = last is None or time.time() - last > 3 * settings.reconcile_interval
        body = {
            "ok": not stale,
            "status": "degraded" if stale else "ok",
            "version": __version__,
        }
        return JSONResponse(body, status_code=503 if stale else 200)

    app.mount(f"{base}/static", StaticFiles(directory=str(STATIC)), name="static")
    return app


def csv_cell(value: object) -> str:
    """A spreadsheet cell that cannot run as a formula."""
    text = "".join(character for character in str(value or "") if ord(character) >= 32)
    text = text.strip()
    if text[:1] in {"=", "+", "-", "@", "'"}:
        text = "'" + text
    return text


def _constant_eq(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode(), b.encode())
