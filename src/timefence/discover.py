"""Discover domains from the AdGuard Home query log."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

log = logging.getLogger("timefence.discover")


# --------------------------------------------------------------------------- #
# Public Suffix List
# --------------------------------------------------------------------------- #


class PublicSuffixList:
    """Offline registrable-domain lookup (exceptions + wildcards supported)."""

    def __init__(self, path: Path | None = None) -> None:
        self.rules: set[str] = set()
        self.wildcards: set[str] = set()
        self.exceptions: set[str] = set()
        file = path or (DATA_DIR / "public_suffix_list.dat")
        try:
            with open(file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("//"):
                        continue
                    if line.startswith("!"):
                        self.exceptions.add(line[1:])
                    elif line.startswith("*."):
                        self.wildcards.add(line[2:])
                    else:
                        self.rules.add(line)
        except FileNotFoundError:  # pragma: no cover - bundled file is always present
            log.warning("public suffix list missing at %s; using last-two-labels fallback", file)

    def _rule_matches(self, labels: list[str]) -> int:
        """Length (in labels) of the longest matching public suffix."""
        best = 0
        # exceptions beat wildcards: publicsuffix.org algorithm
        for size in range(len(labels), 0, -1):
            candidate = ".".join(labels[-size:])
            if candidate in self.exceptions:
                return size - 1
        for size in range(len(labels), 0, -1):
            candidate = ".".join(labels[-size:])
            if candidate in self.rules:
                best = max(best, size)
            if size >= 2 and ".".join(labels[-size + 1 :]) in self.wildcards:
                best = max(best, size)
        return best

    def registrable(self, host: str) -> str:
        host = (host or "").strip().strip(".").lower()
        if not host or host.replace(".", "").isdigit():
            return host
        labels = host.split(".")
        if len(labels) <= 1:
            return host
        suffix_len = self._rule_matches(labels)
        if suffix_len == 0:
            return ".".join(labels[-2:])
        if len(labels) <= suffix_len:
            return host
        return ".".join(labels[-(suffix_len + 1) :])


# --------------------------------------------------------------------------- #
# noise
# --------------------------------------------------------------------------- #


class SuffixList:
    """Simple suffix matcher used for the noise list and managed domains."""

    def __init__(self, entries: list[str]) -> None:
        self.entries = sorted({e.strip().lower().strip(".") for e in entries if e.strip()}, key=len)

    @classmethod
    def from_file(cls, path: Path) -> SuffixList:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return cls([])
        out = []
        for line in lines:
            line = line.split("#", 1)[0].strip().lower()
            if line:
                out.append(line)
        return cls(out)

    def matches(self, host: str) -> bool:
        host = host.strip().lower().strip(".")
        for entry in self.entries:
            if entry.endswith("*"):
                if host.startswith(entry[:-1]):
                    return True
            elif host == entry or host.endswith("." + entry):
                return True
        return False


# --------------------------------------------------------------------------- #
# query log parsing
# --------------------------------------------------------------------------- #


PARSE_TIME_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|z|[+-]\d{2}:?\d{2})?$"
)


def parse_time(value: str) -> int | None:
    """AdGuard sends RFC3339 with nanoseconds and either sign of offset."""
    if not value:
        return None
    match = PARSE_TIME_RE.match(value.strip())
    if not match:
        return None
    head, fraction, offset = match.groups()
    if fraction:
        head = f"{head}.{fraction[:6]}"
    if offset is None or offset in ("Z", "z"):
        offset = "+00:00"
    elif ":" not in offset:
        offset = f"{offset[:3]}:{offset[3:]}"
    try:
        parsed = datetime.fromisoformat(f"{head}{offset}")
    except ValueError:
        return None
    return int(parsed.timestamp())


def aggregate(
    entries: list[dict],
    *,
    window_minutes: int,
    psl: PublicSuffixList,
    noise: SuffixList,
    managed_domains: set[str],
    now: int,
    client: str | None = None,
    show_all: bool = False,
) -> list[dict]:
    since = now - window_minutes * 60
    buckets: dict[str, dict] = {}
    for entry in entries:
        ts = parse_time(entry.get("time", ""))
        if ts is None or ts < since:
            continue
        if client and entry.get("client") != client:
            continue
        question = entry.get("question") or {}
        host = str(question.get("name", "")).strip().lower().rstrip(".")
        if not host:
            continue
        registrable = psl.registrable(host)
        if not registrable:
            continue
        if not show_all:
            if noise.matches(host) or noise.matches(registrable):
                continue
            if any(
                registrable == d or registrable.endswith("." + d) or d.endswith("." + registrable)
                for d in managed_domains
            ):
                continue
        bucket = buckets.setdefault(
            registrable,
            {"domain": registrable, "count": 0, "first_seen": ts, "last_seen": ts, "hosts": set()},
        )
        bucket["count"] += 1
        bucket["first_seen"] = min(bucket["first_seen"], ts)
        bucket["last_seen"] = max(bucket["last_seen"], ts)
        bucket["hosts"].add(host)
    out = []
    for bucket in buckets.values():
        bucket["hosts"] = sorted(bucket["hosts"])[:12]
        out.append(bucket)
    out.sort(key=lambda b: (-b["count"], b["domain"]))
    return out


def recent_clients(entries: list[dict], now: int, limit: int = 25) -> list[dict]:
    seen: dict[str, dict] = {}
    for entry in entries:
        client = entry.get("client")
        if not client:
            continue
        ts = parse_time(entry.get("time", "")) or 0
        info = seen.setdefault(client, {"client": client, "count": 0, "last_seen": ts})
        info["count"] += 1
        info["last_seen"] = max(info["last_seen"], ts)
    ordered = sorted(seen.values(), key=lambda c: -c["last_seen"])[:limit]
    for item in ordered:
        item["last_seen_ago"] = max(0, now - item["last_seen"])
    return ordered


def describe_window(minutes: int, now: int) -> str:
    start = datetime.fromtimestamp(now - minutes * 60, UTC).strftime("%H:%M")
    end = datetime.fromtimestamp(now, UTC).strftime("%H:%M")
    return f"{start}-{end} UTC"


def window_bounds(minutes: int, now: int) -> tuple[int, int]:
    return now - minutes * 60, now


def in_window(ts: int | None, minutes: int, now: int) -> bool:
    if ts is None:
        return False
    return now - minutes * 60 <= ts <= now + 60


def format_ts(ts: int, tz) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M:%S")


def humanize_age(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def window_delta(minutes: int) -> timedelta:
    return timedelta(minutes=minutes)
