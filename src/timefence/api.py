"""FastAPI application: HTTP layer for TimeFence."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import APP_TITLE, __version__, budget, credentials, db, i18n, security
from .adguard import AdGuardClient, AdGuardError
from .config import Settings
from .discover import PublicSuffixList, SuffixList, aggregate, humanize_age, recent_clients
from .reconciler import Reconciler

log = logging.getLogger("timefence.web")

DATA = Path(__file__).parent / "data"
STATIC = Path(__file__).parent / "static"
TEMPLATES = Path(__file__).parent / "templates"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def json_error(status: int, message: str, code: str = "error", **extra: Any) -> JSONResponse:
    return JSONResponse({"error": message, "code": code, **extra}, status_code=status)


def client_ip(request: Request, settings: Settings) -> str:
    return security.client_ip(
        request.client.host if request.client else None,
        dict(request.headers),
        settings.trusted_proxies,
    )


validate_password = credentials.validate_admin_password
validate_pin = credentials.validate_user_pin


# --------------------------------------------------------------------------- #
# application factory
# --------------------------------------------------------------------------- #


def create_app(
    settings: Settings | None = None,
    adguard: AdGuardClient | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    logging.getLogger("timefence").setLevel(settings.log_level)

    base = "" if settings.base_path == "/" else settings.base_path

    conn = db.connect(settings.db_path)
    db.migrate(conn)

    lock = threading.RLock()
    session_manager = security.SessionManager(security.load_or_create_secret(settings.data_dir))
    limiter = security.LoginRateLimiter()
    agh = adguard or AdGuardClient(
        settings.adguard_url, settings.adguard_user, settings.adguard_password
    )
    reconciler = Reconciler(conn, agh, settings.zone)

    psl = PublicSuffixList()
    noise = SuffixList.from_file(DATA / "noise_domains.txt")
    critical_patterns = budget.load_critical_patterns(DATA / "critical_domains.txt")

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html"]),
    )

    services_cache: dict[str, Any] = {"ts": 0.0, "data": []}
    querylog_cache: dict[str, Any] = {"ts": 0.0, "data": []}

    # ---------------------------------------------------------------- setup
    def seed_credentials() -> None:
        with lock:
            rows = {r["role"] for r in conn.execute("SELECT role FROM credentials")}
            now = int(time.time())
            if "admin" not in rows:
                conn.execute(
                    "INSERT INTO credentials(role, hash, updated_at) VALUES(?,?,?)",
                    ("admin", security.hash_secret(credentials.DEFAULT_ADMIN_PASSWORD), now),
                )
                db.set_bool_setting(conn, "setup_complete", False)
            if "user" not in rows:
                conn.execute(
                    "INSERT INTO credentials(role, hash, updated_at) VALUES(?,?,?)",
                    ("user", security.hash_secret(credentials.DEFAULT_USER_PIN), now),
                )
            conn.commit()

    def setup_complete() -> bool:
        return db.get_bool_setting(conn, "setup_complete", False)

    def verify(role: str, secret: str) -> bool:
        return credentials.verify(conn, role, secret)

    def set_credential(role: str, secret: str) -> None:
        credentials.set_secret(conn, role, secret)

    def current_session(request: Request) -> security.SessionData | None:
        return getattr(request.state, "session", None)

    def require(role: str, request: Request) -> security.SessionData:
        sess = current_session(request)
        if not sess:
            raise HTTPException(status_code=401, detail="login required")
        if role == "admin" and sess.role != "admin":
            raise HTTPException(status_code=403, detail="admin required")
        return sess

    def guard_forbidden(request: Request) -> JSONResponse | None:
        """Pre-setup: user PIN disabled and every API call blocked."""
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
            base=settings.base_path,
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
        return budget.group_state(conn, group, settings.zone, now)

    def state_payload(role: str, now: int | None = None) -> dict:
        now = now or int(time.time())
        groups = [group_payload(g, now) for g in budget.list_groups(conn)]
        payload: dict[str, Any] = {
            "app": {
                "name": i18n.t("app.title"),
                "version": __version__,
                "base_path": settings.base_path,
                "timezone": settings.tz,
                "setup_complete": setup_complete(),
                "session_options": [15, 30, 60],
                "role": role,
                "now": now,
            },
            "groups": groups,
            "history": budget.history(conn, settings.zone, days=7, now=now),
            "sessions_today": budget.sessions_today(conn, settings.zone, now),
            "adguard_error": reconciler.last_error,
        }
        return payload

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
                    host = rule.removeprefix("||").removesuffix("^").lstrip(".")
                    if host and "*" not in host:
                        out.add(host)
        return out

    def body(request_body: Any) -> dict:
        return request_body if isinstance(request_body, dict) else {}

    async def read_json(request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------- lifespan
    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        seed_credentials()
        ok, detail = agh.test_connection()
        if ok:
            log.info("AdGuard Home connection OK: %s", detail)
        else:
            log.error("AdGuard Home connection FAILED: %s", detail)
        log.info("configuration: %s", settings.redacted())
        with lock:
            open_sessions = list(conn.execute("SELECT id FROM sessions WHERE ended_at IS NULL"))
            for row in open_sessions:
                budget.close_session(conn, row["id"], "service_restart")
            if open_sessions:
                log.info(
                    "fail-safe: closed %d session(s) left open by a previous run",
                    len(open_sessions),
                )
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
            stop.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            conn.close()

    app = FastAPI(
        title=APP_TITLE,
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.conn = conn
    app.state.adguard = agh
    app.state.reconciler = reconciler

    # ------------------------------------------------------------ middleware
    @app.middleware("http")
    async def middleware(request: Request, call_next):
        request.state.session = session_manager.read(request.cookies.get(session_manager.COOKIE))
        path = request.url.path
        api_prefix = f"{base}/api/"
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and path.startswith(api_prefix):
            exempt = {f"{base}/api/login"}
            sess = request.state.session
            if path not in exempt:
                token = request.headers.get("x-csrf-token", "")
                if not sess or not token or not _constant_eq(token, sess.csrf):
                    return json_error(403, "CSRF token missing or invalid", "csrf")
        blocked = guard_forbidden(request)
        if blocked is not None:
            return blocked
        response = await call_next(request)
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
        if not sess or sess.role != "admin":
            return RedirectResponse(f"{base}/login")
        if setup_complete():
            return RedirectResponse(base + "/")
        return page("setup.html", request)

    # ------------------------------------------------------------------ auth
    @app.post(f"{base}/api/login")
    async def api_login(request: Request, response: Response):
        ip = client_ip(request, settings)
        locked = limiter.locked_for(ip)
        if locked:
            return json_error(
                429,
                i18n.t("login.locked", minutes=max(1, locked // 60 + 1)),
                "locked",
            )
        data = await read_json(request)
        role = str(data.get("role", "user"))
        secret = str(data.get("secret", ""))
        if role not in {"user", "admin"}:
            return json_error(400, "bad role", "bad_role")
        if role == "user" and not setup_complete():
            return json_error(403, i18n.t("error.setup_required"), "setup_required")
        if not verify(role, secret):
            limiter.record_failure(ip)
            budget.log_event(conn, "login_failed", {"role": role, "ip": ip})
            remaining = limiter.locked_for(ip)
            if remaining:
                return json_error(
                    429, i18n.t("login.locked", minutes=max(1, remaining // 60 + 1)), "locked"
                )
            return json_error(401, i18n.t("login.wrong"), "bad_credentials")
        limiter.reset(ip)
        token, csrf = session_manager.issue(role)
        budget.log_event(conn, "login", {"role": role, "ip": ip})
        response = JSONResponse(
            {"ok": True, "role": role, "csrf": csrf, "setup_complete": setup_complete()}
        )
        response.set_cookie(
            session_manager.COOKIE,
            token,
            max_age=settings.session_max_age,
            path=settings.base_path,
            httponly=True,
            samesite="lax",
            secure=settings.secure_cookies,
        )
        return response

    @app.post(f"{base}/api/logout")
    def api_logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(session_manager.COOKIE, path=settings.base_path)
        return response

    @app.post(f"{base}/api/setup")
    async def api_setup(request: Request):
        sess = require("admin", request)
        if setup_complete():
            return json_error(400, "setup already completed", "already_setup")
        data = await read_json(request)
        admin_password = str(data.get("admin_password", ""))
        pin = str(data.get("user_pin", ""))
        problem = validate_password(admin_password) or validate_pin(pin)
        if problem:
            return json_error(400, problem, "invalid")
        with lock:
            set_credential("admin", admin_password)
            set_credential("user", pin)
            db.set_bool_setting(conn, "setup_complete", True)
            budget.log_event(conn, "setup_completed", {"role": sess.role})
        log.info("first-run setup completed")
        return JSONResponse({"ok": True})

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
            "session_options": [15, 30, 60],
            "now": int(time.time()),
        }

    @app.get(f"{base}/api/i18n")
    def api_i18n():
        return i18n.strings()

    # ----------------------------------------------------------------- state
    @app.get(f"{base}/api/state")
    def api_state(request: Request):
        sess = require("user", request)
        payload = state_payload(sess.role)
        payload["app"]["csrf"] = sess.csrf
        return payload

    # -------------------------------------------------------------- sessions
    @app.post(f"{base}/api/session/start")
    async def api_session_start(request: Request):
        sess = require("user", request)
        data = await read_json(request)
        try:
            group_id = int(data.get("group_id"))
            minutes = int(data.get("minutes"))
        except (TypeError, ValueError):
            return json_error(400, "group_id and minutes are required", "bad_request")
        with lock:
            group = budget.get_group(conn, group_id)
            if not group or group.deleted:
                return json_error(404, i18n.t("error.not_found"), "not_found")
            try:
                session = budget.start_session(conn, group, minutes, settings.zone, actor=sess.role)
            except budget.ValidationError as exc:
                message = str(exc)
                code = "no_budget" if "no minutes" in message else "invalid"
                return json_error(409, message, code)
            budget.log_event(
                conn,
                "session_start",
                {
                    "group_id": group.id,
                    "group": group.name,
                    "minutes": session["planned_seconds"] // 60,
                    "actor": sess.role,
                },
            )
            result = reconciler.run("session_start")
        payload = state_payload(sess.role)
        payload["app"]["csrf"] = sess.csrf
        return {"ok": True, "session": dict(session), "reconcile": result, "state": payload}

    @app.post(f"{base}/api/session/stop")
    async def api_session_stop(request: Request):
        sess = require("user", request)
        data = await read_json(request)
        try:
            group_id = int(data.get("group_id"))
        except (TypeError, ValueError):
            return json_error(400, "group_id is required", "bad_request")
        with lock:
            group = budget.get_group(conn, group_id)
            if not group:
                return json_error(404, i18n.t("error.not_found"), "not_found")
            stopped = budget.stop_sessions(conn, group_id, "stopped")
            if stopped:
                budget.log_event(
                    conn,
                    "session_stop",
                    {
                        "group_id": group_id,
                        "group": group.name,
                        "used_seconds": stopped[0]["used_seconds"],
                        "actor": sess.role,
                    },
                )
            result = reconciler.run("session_stop")
        payload = state_payload(sess.role)
        payload["app"]["csrf"] = sess.csrf
        return {
            "ok": True,
            "stopped": [dict(s) for s in stopped],
            "reconcile": result,
            "state": payload,
        }

    # ---------------------------------------------------------------- groups
    @app.get(f"{base}/api/groups")
    def api_groups(request: Request):
        require("user", request)
        now = int(time.time())
        return {"groups": [group_payload(g, now) for g in budget.list_groups(conn)]}

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
        return {
            "name": str(data.get("name", existing.name if existing else "")).strip(),
            "emoji": str(data.get("emoji", existing.emoji if existing else "")).strip(),
            "weekday_minutes": weekday,
            "weekend_minutes": weekend,
            "services": [
                str(s).strip().lower() for s in (data.get("services") or []) if str(s).strip()
            ],
            "domains": domains,
            "paused": bool(data.get("paused", existing.paused if existing else False)),
        }

    @app.post(f"{base}/api/groups")
    async def api_group_create(request: Request):
        sess = require("admin", request)
        data = await read_json(request)
        fields = group_from_payload(data)
        hits = critical_hits(fields["domains"])
        if hits and not data.get("confirm_critical"):
            return json_error(
                409,
                i18n.t("groups.critical_title"),
                "critical_domains",
                critical=hits,
            )
        with lock:
            try:
                group = budget.create_group(conn, **fields)
            except budget.ValidationError as exc:
                return json_error(400, str(exc), "invalid")
            budget.log_event(conn, "group_create", {"group": group.name, "actor": sess.role})
            reconciler.run("group_create")
        return {"ok": True, "group": group_payload(group, int(time.time()))}

    @app.put(f"{base}/api/groups/{{group_id}}")
    async def api_group_update(group_id: int, request: Request):
        sess = require("admin", request)
        data = await read_json(request)
        with lock:
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
            except budget.ValidationError as exc:
                return json_error(400, str(exc), "invalid")
            budget.log_event(conn, "group_update", {"group": group.name, "actor": sess.role})
            reconciler.run("group_update")
        return {"ok": True, "group": group_payload(group, int(time.time()))}

    @app.delete(f"{base}/api/groups/{{group_id}}")
    def api_group_delete(group_id: int, request: Request):
        sess = require("admin", request)
        with lock:
            group = budget.get_group(conn, group_id)
            if not group or group.deleted:
                return json_error(404, i18n.t("error.not_found"), "not_found")
            budget.stop_sessions(conn, group_id, "deleted")
            name = budget.delete_group(conn, group_id)
            budget.log_event(conn, "group_delete", {"group": name, "actor": sess.role})
            reconciler.run("group_delete")
        return {"ok": True, "deleted": name}

    @app.post(f"{base}/api/groups/{{group_id}}/pause")
    async def api_group_pause(group_id: int, request: Request):
        sess = require("admin", request)
        data = await read_json(request)
        paused = bool(data.get("paused", True))
        with lock:
            try:
                group = budget.update_group(conn, group_id, paused=paused)
            except budget.ValidationError as exc:
                return json_error(400, str(exc), "invalid")
            if paused:
                budget.stop_sessions(conn, group_id, "admin_block")
            budget.log_event(
                conn, "group_pause", {"group": group.name, "paused": paused, "actor": sess.role}
            )
            reconciler.run("group_pause")
        return {"ok": True, "group": group_payload(group, int(time.time()))}

    @app.post(f"{base}/api/groups/{{group_id}}/bonus")
    async def api_group_bonus(group_id: int, request: Request):
        sess = require("admin", request)
        data = await read_json(request)
        try:
            minutes = int(data.get("minutes", 15))
        except (TypeError, ValueError):
            return json_error(400, "minutes is required", "bad_request")
        with lock:
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
        return {"ok": True, "group": group_payload(group, int(time.time()))}

    @app.post(f"{base}/api/groups/{{group_id}}/free")
    async def api_group_free(group_id: int, request: Request):
        sess = require("admin", request)
        data = await read_json(request)
        with lock:
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
            reconciler.run("admin_free")
        return {"ok": True, "until": until, "group": group_payload(group, int(time.time()))}

    @app.post(f"{base}/api/groups/{{group_id}}/free/clear")
    def api_group_free_clear(group_id: int, request: Request):
        sess = require("admin", request)
        with lock:
            group = budget.get_group(conn, group_id)
            if not group or group.deleted:
                return json_error(404, i18n.t("error.not_found"), "not_found")
            budget.clear_free(conn, group_id)
            budget.log_event(conn, "admin_free_clear", {"group": group.name, "actor": sess.role})
            reconciler.run("admin_free_clear")
        return {"ok": True, "group": group_payload(group, int(time.time()))}

    @app.post(f"{base}/api/groups/{{group_id}}/block")
    def api_group_block(group_id: int, request: Request):
        sess = require("admin", request)
        with lock:
            group = budget.get_group(conn, group_id)
            if not group or group.deleted:
                return json_error(404, i18n.t("error.not_found"), "not_found")
            budget.clear_free(conn, group_id)
            stopped = budget.stop_sessions(conn, group_id, "admin_block")
            budget.log_event(conn, "admin_block", {"group": group.name, "actor": sess.role})
            reconciler.run("admin_block")
        return {
            "ok": True,
            "stopped": [dict(s) for s in stopped],
            "group": group_payload(group, int(time.time())),
        }

    # ------------------------------------------------------------------ test
    @app.get(f"{base}/api/groups/{{group_id}}/test")
    def api_group_test(group_id: int, request: Request):
        require("admin", request)
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
                for rule in (services.get(service_id, {}).get("rules") or [])[:2]:
                    host = rule.removeprefix("||").removesuffix("^").lstrip(".")
                    if host and "*" not in host:
                        hostnames.append(host)
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
                    "blocked": reason
                    not in {
                        "",
                        "NotFilteredNotFound",
                        "NotFilteredWhiteList",
                        "NotFilteredAllowList",
                    },
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
        used = {s: g.name for g in budget.list_groups(conn) for s in g.services}
        out = [
            {
                "id": s["id"],
                "name": s.get("name", s["id"]),
                "icon_svg": s.get("icon_svg", ""),
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
        if now - querylog_cache["ts"] > 20:
            try:
                querylog_cache["data"] = agh.querylog(limit=2000)
            except AdGuardError as exc:
                return json_error(502, i18n.t("error.adguard", detail=str(exc)), "adguard")
            querylog_cache["ts"] = now
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
    async def api_discover_add(request: Request):
        sess = require("admin", request)
        data = await read_json(request)
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
        with lock:
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
            reconciler.run("discover_add")
        return {"ok": True, "group": group_payload(group, int(time.time())), "added": domains}

    # -------------------------------------------------------------- admin-ish
    @app.post(f"{base}/api/admin/credentials")
    async def api_admin_credentials(request: Request):
        sess = require("admin", request)
        data = await read_json(request)
        role = str(data.get("role", ""))
        secret = str(data.get("secret", ""))
        if role == "admin":
            problem = validate_password(secret)
        elif role == "user":
            problem = validate_pin(secret)
        else:
            return json_error(400, "bad role", "bad_role")
        if problem:
            return json_error(400, problem, "invalid")
        with lock:
            set_credential(role, secret)
            budget.log_event(conn, "credentials_changed", {"role": role, "actor": sess.role})
        return {"ok": True}

    @app.post(f"{base}/api/adguard/test")
    def api_adguard_test(request: Request):
        require("admin", request)
        ok, detail = agh.test_connection()
        return {"ok": ok, "detail": detail}

    @app.get(f"{base}/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    app.mount(f"{base}/static", StaticFiles(directory=str(STATIC)), name="static")
    return app


def _constant_eq(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode(), b.encode())
