"""Outbound alerts: one Telegram, ntfy or webhook channel, one worker.

The worker never blocks a request, a reconcile run or shutdown, and it never
lets a transport problem escape as an exception. Failures leave this module only
as a short category, and no URL, path, topic, token or chat id is ever logged or
read back through the API.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import re
import socket
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from datetime import time as dtime
from typing import Any
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import requests

from . import budget, db, i18n

log = logging.getLogger("timefence.alerts")
# urllib3 logs the request line at DEBUG, and a Telegram token or a webhook
# secret lives in that path. The transport must never print it.
logging.getLogger("urllib3").setLevel(logging.WARNING)

EVENT_NAMES = (
    "group_low",
    "group_exhausted",
    "bedtime_started",
    "sync_failing",
    "sync_recovered",
    "weekly_summary",
)
DEFAULT_OFF = frozenset({"bedtime_started", "weekly_summary"})
CHANNELS = ("telegram", "ntfy", "webhook")
LOW_SECONDS = 600
HOURLY_LIMIT = 20
QUEUE_SIZE = 50
PRUNE_AFTER = 14 * 86400
BODY_LIMIT = 1024
DELIVERY_DEADLINE = 5.0  # a whole delivery, headers and body, may take this long
DEFAULT_NTFY_BASE = "https://ntfy.sh"
PROTECTED_CLASSES = frozenset({"ex", "sync_failing"})
TEST_LOCK = threading.Lock()

TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
TELEGRAM_TOKEN_RE = re.compile(r"^[0-9]{5,16}:[A-Za-z0-9_-]{30,50}$")
TELEGRAM_CHAT_RE = re.compile(r"^-?[0-9]{1,20}$")

CHANNEL = "alerts.channel"
ENABLED = "alerts.enabled"
NTFY_BASE = "alerts.ntfy_base"
NTFY_TOPIC = "alerts.ntfy_topic"
NTFY_TOKEN = "alerts.ntfy_token"
TELEGRAM_TOKEN = "alerts.telegram_token"
TELEGRAM_CHAT = "alerts.telegram_chat"
WEBHOOK_URL = "alerts.webhook_url"
EVENT_PREFIX = "alerts.event."
OUTAGE = "alerts.outage_started_at"
DROPPED = "alerts.dropped"
DROPPED_LOG = "alerts.dropped_log"
LAST_RESULT = "alerts.last_result"
LAST_RESULT_TS = "alerts.last_result_ts"

PRIORITY = {
    "group_low": "default",
    "group_exhausted": "high",
    "bedtime_started": "default",
    "sync_failing": "high",
    "sync_recovered": "default",
    "weekly_summary": "low",
    "test": "default",
}
TAGS = {
    "group_low": "hourglass",
    "group_exhausted": "no_entry",
    "bedtime_started": "crescent_moon",
    "sync_failing": "warning",
    "sync_recovered": "white_check_mark",
    "weekly_summary": "bar_chart",
    "test": "bell",
}

SESSION = requests.Session()
SESSION.trust_env = False  # a proxy from the environment must not see the token


class TopicError(budget.ValidationError):
    """The ntfy topic cannot be used."""


@dataclass(frozen=True)
class Target:
    """One validated delivery target; the only way to build one is parse_target."""

    url: str
    channel: str = ""
    host: str = ""
    port: int | None = None
    token: str = ""
    chat: str = ""  # telegram only; it travels in the JSON body, never the URL


@dataclass(frozen=True)
class Message:
    event: str
    key: str
    created_at: int = 0
    time_iso: str = ""
    group: str = ""
    minutes_left: int = 0
    time_text: str = ""
    outage_seconds: int = 0
    groups: tuple[tuple[str, float], ...] = field(default_factory=tuple)


def parse_target(raw: str) -> Target:
    """Validate an admin supplied URL; anything doubtful is refused."""
    text = str(raw or "").strip()
    if not text:
        raise budget.ValidationError("an alert URL is required")
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"}:
        raise budget.ValidationError("only http:// and https:// are supported")
    if "@" in parts.netloc:
        raise budget.ValidationError("credentials in the URL are not supported")
    if parts.fragment:
        raise budget.ValidationError("the URL must not carry a fragment")
    host = parts.hostname
    if not host:
        raise budget.ValidationError("the URL needs a host")
    if "%" in host or any(character.isspace() for character in host):
        raise budget.ValidationError("the host is not supported")
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise budget.ValidationError("the host is not supported") from exc
    ipv6 = parts.netloc.startswith("[") and ":" in host
    if not (HOST_RE.match(ascii_host) or ipv6):
        raise budget.ValidationError("the host is not supported")
    try:
        port = parts.port
    except ValueError as exc:
        raise budget.ValidationError("the port is not supported") from exc
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return Target(url=text, host=host, port=port)


# --------------------------------------------------------------------------- #
# configuration (settings keys, never an events row)
# --------------------------------------------------------------------------- #


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise budget.ValidationError("expected a boolean")


def read_config(conn: sqlite3.Connection) -> dict:
    rows = {
        row["key"]: row["value"]
        for row in conn.execute("SELECT key, value FROM settings WHERE key LIKE 'alerts.%'")
    }
    return {
        "channel": (rows.get(CHANNEL) or "").strip(),
        "enabled": (rows.get(ENABLED) or "1") != "0",
        "events": {
            name: (rows.get(EVENT_PREFIX + name) or ("0" if name in DEFAULT_OFF else "1")) == "1"
            for name in EVENT_NAMES
        },
        "ntfy": {
            "base": rows.get(NTFY_BASE) or "",
            "topic": rows.get(NTFY_TOPIC) or "",
            "token": rows.get(NTFY_TOKEN) or "",
        },
        "telegram": {
            "token": rows.get(TELEGRAM_TOKEN) or "",
            "chat": rows.get(TELEGRAM_CHAT) or "",
        },
        "webhook": rows.get(WEBHOOK_URL) or "",
    }


def write_config(conn: sqlite3.Connection, data: dict) -> None:
    """Merge a payload: an absent key keeps the stored value, "" clears it."""
    updates: dict[str, str] = {}
    if "channel" in data:
        channel = str(data.get("channel") or "").strip().lower()
        if channel and channel not in CHANNELS:
            raise budget.ValidationError("unknown channel")
        updates[CHANNEL] = channel
    if "enabled" in data:
        updates[ENABLED] = "1" if _as_bool(data.get("enabled")) else "0"
    if "ntfy_base" in data:
        raw = str(data.get("ntfy_base") or "").strip()
        if raw:
            parse_target(raw)
        updates[NTFY_BASE] = raw
    if "ntfy_topic" in data:
        raw = str(data.get("ntfy_topic") or "").strip()
        if raw and not TOPIC_RE.match(raw):
            raise TopicError("the topic must be 1-64 letters, digits, dashes or underscores")
        updates[NTFY_TOPIC] = raw
    if "ntfy_token" in data:
        raw = str(data.get("ntfy_token") or "").strip()
        if raw and any(ord(character) < 33 or ord(character) > 126 for character in raw):
            raise budget.ValidationError("the token must be printable ASCII")
        updates[NTFY_TOKEN] = raw
    if "telegram_token" in data:
        raw = str(data.get("telegram_token") or "").strip()
        if raw and not TELEGRAM_TOKEN_RE.match(raw):
            raise budget.ValidationError("the bot token is not a Telegram bot token")
        updates[TELEGRAM_TOKEN] = raw
    if "telegram_chat" in data:
        raw = str(data.get("telegram_chat") or "").strip()
        if raw and not TELEGRAM_CHAT_RE.match(raw):
            raise budget.ValidationError("the chat id must be a number")
        updates[TELEGRAM_CHAT] = raw
    if "webhook_url" in data:
        raw = str(data.get("webhook_url") or "").strip()
        if raw:
            parse_target(raw)
        updates[WEBHOOK_URL] = raw
    if "events" in data:
        events = data.get("events")
        if not isinstance(events, dict):
            raise budget.ValidationError("events must be an object")
        for name in EVENT_NAMES:
            if name in events:
                updates[EVENT_PREFIX + name] = "1" if _as_bool(events[name]) else "0"
    for key, value in updates.items():
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
    conn.commit()


def _host_display(raw: str) -> str:
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    host = parts.hostname or ""
    if not host:
        return ""
    if ":" in host:  # an IPv6 literal needs its brackets back
        return f"[{host}]:{parts.port}" if parts.port else f"[{host}]"
    return f"{host}:{parts.port}" if parts.port else host


def _tail(value: str, size: int = 2) -> str:
    return value[-size:] if value else ""


def _recent_drops(conn: sqlite3.Connection, now: int) -> list[int]:
    try:
        entries = json.loads(db.get_setting(conn, DROPPED_LOG, "[]") or "[]")
    except (TypeError, ValueError):
        return []
    recent = []
    for entry in entries:
        try:
            stamp = int(entry)
        except (TypeError, ValueError):
            continue
        if stamp > now - 3600:
            recent.append(stamp)
    return recent


def _count_dropped(conn: sqlite3.Connection, now: int) -> None:
    recent = _recent_drops(conn, now)
    recent.append(now)
    db.set_setting(conn, DROPPED_LOG, json.dumps(recent))
    total = int(db.get_setting(conn, DROPPED, "0") or 0) + 1
    db.set_setting(conn, DROPPED, str(total))


def _reserve(conn: sqlite3.Connection, key: str, now: int) -> bool:
    """Write the de-duplication and rate-limit row, or report false."""
    if conn.execute("SELECT 1 FROM alerts_sent WHERE key=?", (key,)).fetchone():
        return False
    rows = conn.execute("SELECT key FROM alerts_sent WHERE sent_at > ?", (now - 3600,)).fetchall()
    keys = [str(row["key"]) for row in rows]
    if len(keys) >= HOURLY_LIMIT:
        prefix = key.split(":", 1)[0]
        own = sum(1 for existing in keys if existing.split(":", 1)[0] == prefix)
        if prefix not in PROTECTED_CLASSES or own >= HOURLY_LIMIT:
            _count_dropped(conn, now)
            return False
    # the primary key is the real guard: a duplicate can never be sent twice
    cursor = conn.execute("INSERT OR IGNORE INTO alerts_sent(key, sent_at) VALUES(?,?)", (key, now))
    reserved = cursor.rowcount > 0
    conn.commit()
    return reserved


def probe_groups(conn: sqlite3.Connection) -> list[budget.Group]:
    """Just what the probe needs: no service or domain links are loaded."""
    windows: dict[int, list[budget.Window]] = {}
    for row in conn.execute("SELECT group_id, start_min, end_min, days FROM group_windows"):
        windows.setdefault(int(row["group_id"]), []).append(
            budget.Window(int(row["start_min"]), int(row["end_min"]), str(row["days"]))
        )
    groups: list[budget.Group] = []
    for row in conn.execute(
        "SELECT id, name, emoji, weekday_minutes, weekend_minutes, paused, "
        "force_block_until, owner_id FROM groups WHERE deleted=0 ORDER BY name"
    ):
        gid = int(row["id"])
        groups.append(
            budget.Group(
                id=gid,
                name=str(row["name"]),
                emoji=str(row["emoji"]),
                weekday_minutes=int(row["weekday_minutes"]),
                weekend_minutes=int(row["weekend_minutes"]),
                paused=bool(row["paused"]),
                deleted=False,
                force_block_until=int(row["force_block_until"] or 0),
                owner_id=row["owner_id"],
                windows=windows.get(gid, []),
            )
        )
    return groups


def public_config(conn: sqlite3.Connection, now: int) -> dict:
    """What GET /api/alerts may show: counters, toggles and two-character tails."""
    config = read_config(conn)
    rows = conn.execute("SELECT key FROM alerts_sent WHERE sent_at > ?", (now - 3600,)).fetchall()
    keys = [str(row["key"]) for row in rows]
    classes = Counter(key.split(":", 1)[0] for key in keys)
    topic = config["ntfy"]["topic"]
    path = ""
    if config["webhook"]:
        with contextlib.suppress(ValueError):
            path = urlsplit(config["webhook"]).path
    last_ts = db.get_setting(conn, LAST_RESULT_TS, "")
    return {
        "channel": config["channel"],
        "enabled": config["enabled"],
        "events": config["events"],
        "ntfy": {
            "base_host": _host_display(config["ntfy"]["base"]),
            "topic_tail": _tail(topic),
            "token_configured": bool(config["ntfy"]["token"]),
        },
        "telegram": {
            "token_tail": _tail(config["telegram"]["token"]),
            "chat_tail": _tail(config["telegram"]["chat"]),
            "configured": bool(config["telegram"]["token"] and config["telegram"]["chat"]),
        },
        "webhook": {"host": _host_display(config["webhook"]), "path_tail": _tail(path)},
        "sent_last_hour": len(keys),
        "dropped_last_hour": len(_recent_drops(conn, now)),
        "classes": dict(classes),
        "last_result": db.get_setting(conn, LAST_RESULT, ""),
        "last_result_ts": int(last_ts) if last_ts else None,
    }


# --------------------------------------------------------------------------- #
# message building (every user-facing string comes from i18n)
# --------------------------------------------------------------------------- #


def message_text(message: Message) -> str:
    if message.event == "group_low":
        return i18n.t("alert.low", group=message.group, minutes=message.minutes_left)
    if message.event == "group_exhausted":
        return i18n.t("alert.exhausted", group=message.group)
    if message.event == "bedtime_started":
        return i18n.t("alert.bedtime_started", group=message.group, time=message.time_text)
    if message.event == "sync_failing":
        return i18n.t("alert.sync_failing")
    if message.event == "sync_recovered":
        return i18n.t("alert.sync_recovered")
    if message.event == "weekly_summary":
        lines = [i18n.t("alert.weekly")]
        lines += [
            i18n.t("alert.weekly_line", group=name, minutes=minutes)
            for name, minutes in message.groups
        ]
        return "\n".join(lines)
    return i18n.t("alert.test")


def webhook_payload(message: Message) -> dict:
    if message.event in {"group_low", "group_exhausted"}:
        return {
            "event": message.event,
            "group": message.group,
            "minutes_left": message.minutes_left,
            "time": message.time_iso,
            "message": message_text(message),
        }
    if message.event == "bedtime_started":
        return {
            "event": message.event,
            "group": message.group,
            "time": message.time_iso,
            "message": message_text(message),
        }
    if message.event in {"sync_failing", "sync_recovered"}:
        return {
            "event": message.event,
            "time": message.time_iso,
            "message": message_text(message),
            "outage_seconds": message.outage_seconds,
        }
    if message.event == "weekly_summary":
        return {
            "event": "weekly_summary",
            "time": message.time_iso,
            "days": 7,
            "groups": [{"group": name, "minutes": minutes} for name, minutes in message.groups],
        }
    return {"event": message.event, "time": message.time_iso, "message": message_text(message)}


def _target_for(config: dict) -> Target | None:
    with contextlib.suppress(budget.ValidationError):
        if config["channel"] == "ntfy":
            topic = config["ntfy"]["topic"]
            if not topic:
                return None
            base = (config["ntfy"]["base"] or DEFAULT_NTFY_BASE).rstrip("/")
            target = parse_target(f"{base}/{quote(topic, safe='')}")
            return replace(target, channel="ntfy", token=config["ntfy"]["token"])
        if config["channel"] == "telegram":
            token = config["telegram"]["token"]
            chat = config["telegram"]["chat"]
            # write_config validated these; a row edited by hand must not
            # reshape the request path, so re-check before building the URL.
            if not (TELEGRAM_TOKEN_RE.match(token) and TELEGRAM_CHAT_RE.match(chat)):
                return None
            # A fixed host and a fixed path: no admin-supplied URL is involved.
            return Target(
                url=f"https://api.telegram.org/bot{token}/sendMessage",
                channel="telegram",
                host="api.telegram.org",
                port=443,
                chat=chat,
            )
        if config["channel"] == "webhook":
            url = config["webhook"]
            if not url:
                return None
            return replace(parse_target(url), channel="webhook")
    return None


# --------------------------------------------------------------------------- #
# delivery (the only place that opens a socket)
# --------------------------------------------------------------------------- #


def _dns_failure(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, socket.gaierror):
            return True
        if type(current).__name__ == "NameResolutionError":
            return True
        current = current.__cause__ or current.__context__
    return False


def _headers_for(target: Target, message: Message) -> dict[str, str]:
    headers = {
        "Title": f"TimeFence {message.event}",
        "Priority": PRIORITY.get(message.event, "default"),
        "Tags": TAGS.get(message.event, "bell"),
    }
    if target.token:
        headers["Authorization"] = f"Bearer {target.token}"
    return headers


def _request_parts(target: Target, message: Message) -> tuple[bytes, dict[str, str]]:
    """The body and headers for one message, whatever the channel."""
    if target.channel == "ntfy":
        return message_text(message).encode("utf-8"), _headers_for(target, message)
    if target.channel == "telegram":
        body = json.dumps(
            {
                "chat_id": target.chat,
                "text": message_text(message),
                "disable_notification": PRIORITY.get(message.event, "default")
                in {"low", "default"},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        return body, {"Content-Type": "application/json"}
    body = json.dumps(webhook_payload(message), ensure_ascii=False).encode("utf-8")
    return body, {"Content-Type": "application/json"}


def _post(target: Target, payload: bytes, headers: dict[str, str]) -> str:
    """Send one request; returns a category, never an exception and never the body."""
    try:
        response = SESSION.post(
            target.url,
            data=payload,
            headers=headers,
            timeout=(3, 2),
            allow_redirects=False,
            stream=True,
        )
    except requests.exceptions.Timeout:
        return "timeout"
    except requests.exceptions.ConnectionError as exc:
        return "dns" if _dns_failure(exc) else "connect"
    except Exception:  # noqa: BLE001 - a transport message must never surface
        return "connect"
    try:
        status = response.status_code
    finally:
        with contextlib.suppress(Exception):
            if response.raw is not None:
                response.raw.read(BODY_LIMIT)
        response.close()
    if 200 <= status < 300:
        return "ok"
    if 300 <= status < 400:
        return "http_3xx"
    return f"http_{status}"


def _deliver(target: Target, message: Message) -> str:
    """One attempt with a hard wall-clock deadline.

    The socket timeouts only bound each read; a server that trickles a body can
    hold the worker far longer, so the request runs on a helper thread and is
    abandoned after DELIVERY_DEADLINE. The category is the same shape as
    before, and the thread never touches the database.
    """
    payload, headers = _request_parts(target, message)
    outcome: list[str] = []

    def run() -> None:
        try:
            outcome.append(_post(target, payload, headers))
        except Exception:  # noqa: BLE001 - a broken transport must never surface
            outcome.append("connect")

    thread = threading.Thread(target=run, name="timefence-alert-delivery", daemon=True)
    thread.start()
    thread.join(DELIVERY_DEADLINE)
    if thread.is_alive():
        return "timeout"
    return outcome[0] if outcome else "connect"


def _iso(now: int, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(now, tz).isoformat(timespec="seconds")


def _record_result(store: db.Store, category: str, now: int) -> None:
    try:
        with store as conn:
            db.set_setting(conn, LAST_RESULT, category)
            db.set_setting(conn, LAST_RESULT_TS, str(int(now)))
    except (sqlite3.ProgrammingError, RuntimeError):
        log.warning("alert worker stopped: the database is closed")


def send_test(store: db.Store, settings: Any, now: int | None = None) -> str:
    """Deliver one test message synchronously; returns ok or a category."""
    now = int(now if now is not None else time.time())
    with store as conn:
        config = read_config(conn)
        if not config["enabled"] or not config["channel"]:
            return "config"
        target = _target_for(config)
        if target is None:
            return "config"
        key = f"test:{time.time_ns()}"
        if not _reserve(conn, key, now):
            return "rate_limited"
    message = Message(event="test", key=key, created_at=now, time_iso=_iso(now, settings.zone))
    try:
        category = _deliver(target, message)
    except Exception:  # noqa: BLE001 - a broken transport is a category, not a crash
        category = "connect"
    _record_result(store, category, now)
    return category


# --------------------------------------------------------------------------- #
# the worker
# --------------------------------------------------------------------------- #


class AlertWorker:
    """One daemon thread: bounded queue, one attempt per message, no surprises."""

    def __init__(self, store: db.Store, settings: Any) -> None:
        self.store = store
        self.settings = settings
        self.queue: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="timefence-alerts", daemon=True)
        self._stale_runs = 0
        self._last_prune = 0.0
        self._failing = False

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self.thread.start()

    def wake(self) -> None:
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            with self.queue.mutex:
                self.queue.queue.clear()
            self.queue.put_nowait(None)

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout)

    def is_alive(self) -> bool:
        return self.thread.is_alive()

    # -- queue -----------------------------------------------------------
    def enqueue(self, message: Message) -> bool:
        try:
            self.queue.put_nowait(message)
            return True
        except queue.Full:
            evicted = False
            with self.queue.mutex:
                for index, existing in enumerate(self.queue.queue):
                    if getattr(existing, "event", None) == message.event:
                        del self.queue.queue[index]
                        self.queue.queue.append(message)
                        evicted = True
                        break
            self._drop()
            return evicted

    def _drop(self) -> None:
        try:
            with self.store as conn:
                _count_dropped(conn, int(time.time()))
        except (sqlite3.ProgrammingError, RuntimeError):
            return

    def prune(self, now: int) -> bool:
        try:
            with self.store as conn:
                conn.execute("DELETE FROM alerts_sent WHERE sent_at < ?", (now - PRUNE_AFTER,))
                conn.commit()
        except (sqlite3.ProgrammingError, RuntimeError):
            log.warning("alert worker stopped: the database is closed")
            return False
        self._last_prune = float(now)
        return True

    # -- probe -----------------------------------------------------------
    def probe(self, now: int, last_ok_ts: float | None, last_ok_mono: float | None) -> None:
        try:
            self._probe(now, last_ok_ts, last_ok_mono)
        except Exception as exc:  # noqa: BLE001 - the probe must never fail a run
            log.warning("alert probe failed: %s", type(exc).__name__)

    def _probe(self, now: int, last_ok_ts: float | None, last_ok_mono: float | None) -> None:
        messages: list[Message] = []
        with self.store as conn:
            config = read_config(conn)
            if not config["enabled"] or not config["channel"]:
                return
            tz = self.settings.zone
            day = budget.day_str(now, tz)
            for group in probe_groups(conn):
                status, blocked_until = budget.group_status(conn, group, tz, now)
                remaining = budget.remaining_seconds(conn, group, tz, now)
                event = self._event_for(
                    conn, config, group, status, remaining, blocked_until, day, now, tz
                )
                if event is not None:
                    messages.append(event)
            self._probe_outage(conn, config, now, last_ok_ts, last_ok_mono, messages, tz)
            self._probe_weekly(conn, config, now, messages, tz)
        for message in messages:
            self.enqueue(message)

    def _event_for(
        self,
        conn: sqlite3.Connection,
        config: dict,
        group: budget.Group,
        status: str,
        remaining: int,
        blocked_until: int | None,
        day: str,
        now: int,
        tz: ZoneInfo,
    ) -> Message | None:
        if status in budget.PERMITTED_STATUSES:
            # A warning while the group is free would be noise: the free window
            # is an admin decision, not the budget running out.
            wanted = config["events"]["group_low"]
            if status == "allowed" and 0 < remaining <= LOW_SECONDS and wanted:
                key = f"low:{group.id}:{day}"
                if _reserve(conn, key, now):
                    minutes = max(1, -(-remaining // 60))
                    return Message(
                        event="group_low",
                        key=key,
                        created_at=now,
                        time_iso=_iso(now, tz),
                        group=group.name,
                        minutes_left=minutes,
                    )
            return None
        if status == "limit_reached" and config["events"]["group_exhausted"]:
            key = f"ex:{group.id}:{day}"
            if _reserve(conn, key, now):
                return Message(
                    event="group_exhausted",
                    key=key,
                    created_at=now,
                    time_iso=_iso(now, tz),
                    group=group.name,
                )
        if status == "bedtime" and config["events"]["bedtime_started"]:
            # One night, one message: a window that crosses midnight must not
            # fire again after 00:00, so the key names the window's end.
            night = blocked_until if blocked_until is not None else day
            key = f"bedtime:{group.id}:{night}"
            if _reserve(conn, key, now):
                return Message(
                    event="bedtime_started",
                    key=key,
                    created_at=now,
                    time_iso=_iso(now, tz),
                    group=group.name,
                    time_text=budget.clock_str(blocked_until, tz) if blocked_until else "",
                )
        return None

    def _probe_outage(
        self,
        conn: sqlite3.Connection,
        config: dict,
        now: int,
        last_ok_ts: float | None,
        last_ok_mono: float | None,
        messages: list[Message],
        tz: ZoneInfo,
    ) -> None:
        interval = self.settings.reconcile_interval
        stale = (
            last_ok_ts is None
            or last_ok_mono is None
            or (time.monotonic() - last_ok_mono) > 3 * interval
        )
        started = db.get_setting(conn, OUTAGE, "") or ""
        if stale:
            self._stale_runs += 1
            if self._stale_runs >= 3 and not started:
                db.set_setting(conn, OUTAGE, str(now))
                if config["events"]["sync_failing"]:
                    key = f"sync_failing:{now}"
                    if _reserve(conn, key, now):
                        messages.append(
                            Message(
                                event="sync_failing",
                                key=key,
                                created_at=now,
                                time_iso=_iso(now, tz),
                            )
                        )
            return
        self._stale_runs = 0
        if started:
            db.set_setting(conn, OUTAGE, "")
            if config["events"]["sync_recovered"]:
                key = f"sync_recovered:{started}"
                if _reserve(conn, key, now):
                    try:
                        seconds = max(0, now - int(started))
                    except ValueError:
                        seconds = 0
                    messages.append(
                        Message(
                            event="sync_recovered",
                            key=key,
                            created_at=now,
                            time_iso=_iso(now, tz),
                            outage_seconds=seconds,
                        )
                    )

    def _probe_weekly(
        self,
        conn: sqlite3.Connection,
        config: dict,
        now: int,
        messages: list[Message],
        tz: ZoneInfo,
    ) -> None:
        if not config["events"]["weekly_summary"]:
            return
        today = budget.today(tz, now)
        monday = today - timedelta(days=today.weekday())
        key = f"weekly:{monday.isoformat()}"
        if today.weekday() == 0:
            if datetime.fromtimestamp(now, tz).hour < 8:
                return
            if conn.execute("SELECT 1 FROM alerts_sent WHERE key=?", (key,)).fetchone():
                return
            totals = self._weekly_totals(conn, today)
            if _reserve(conn, key, now):
                messages.append(
                    Message(
                        event="weekly_summary",
                        key=key,
                        created_at=now,
                        time_iso=_iso(now, tz),
                        groups=tuple(totals),
                    )
                )
            return
        if not conn.execute("SELECT 1 FROM alerts_sent WHERE key=?", (key,)).fetchone():
            conn.execute("INSERT INTO alerts_sent(key, sent_at) VALUES(?,?)", (key, now))
            conn.commit()
            log.info("missed weekly summary")

    def _weekly_totals(self, conn: sqlite3.Connection, today: Any) -> list[tuple[str, float]]:
        yesterday = today - timedelta(days=1)
        anchor = int(
            datetime.combine(yesterday, dtime(12, 0), tzinfo=self.settings.zone).timestamp()
        )
        rows = budget.usage_range(conn, self.settings.zone, 7, anchor)
        totals: dict[str, int] = {}
        for row in rows:
            totals[row["group"]] = totals.get(row["group"], 0) + int(row["used_seconds"])
        return [(name, round(seconds / 60, 1)) for name, seconds in totals.items()]

    # -- delivery --------------------------------------------------------
    def _deliver_message(self, message: Message) -> bool:
        try:
            with self.store as conn:
                config = read_config(conn)
        except (sqlite3.ProgrammingError, RuntimeError):
            log.warning("alert worker stopped: the database is closed")
            return False
        if not config["enabled"] or not config["channel"]:
            return True
        target = _target_for(config)
        if target is None:
            category = "config"
        else:
            try:
                category = _deliver(target, message)
            except Exception:  # noqa: BLE001 - a broken transport must not kill the thread
                category = "connect"
        _record_result(self.store, category, time.time())
        self._log_outcome(config["channel"], category)
        return True

    def _log_outcome(self, channel: str, category: str) -> None:
        if category == "ok":
            if self._failing:
                self._failing = False
                log.info("alert delivery recovered (%s)", channel)
            return
        if not self._failing:
            self._failing = True
            log.warning("alert delivery failed: %s (%s)", channel, category)

    def _loop(self) -> None:
        if not self.prune(int(time.time())):
            return
        while True:
            item = self.queue.get()
            if item is None or self.stop.is_set():
                return
            now = int(time.time())
            if now - self._last_prune >= 3600 and not self.prune(now):
                return
            if not self._deliver_message(item):
                return
