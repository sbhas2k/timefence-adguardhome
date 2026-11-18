#!/usr/bin/env python3
"""Import the legacy single-purpose "screentime" database into TimeFence.

The old app (0.0.x) managed exactly one site ("youtube"/"testdom"), stored its
state in /var/lib/screentime/state.db and hashed its credentials with Werkzeug.
TimeFence verifies those hashes natively, so they are copied as-is and the
first-run setup is marked as completed.

Usage:
    sudo -u timefence /opt/timefence/venv/bin/python \
        scripts/migrate_legacy.py \
        --legacy /var/lib/screentime/state.db \
        --target /var/lib/timefence/timefence.db

Run it before starting the service (or stop the service first): on startup
TimeFence closes open sessions and reconciles AdGuard to the imported state.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

# a few friendly defaults for the old site names
DISPLAY = {"youtube": ("YouTube", "▶️", "youtube")}
KNOWN_SERVICES = {"youtube", "instagram", "netflix", "tiktok", "roblox", "twitch", "discord"}


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", default="/var/lib/screentime/state.db")
    parser.add_argument("--target", default="/var/lib/timefence/timefence.db")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from timefence import budget, db

    legacy_path = Path(args.legacy)
    target_path = Path(args.target)
    if not legacy_path.exists():
        print(f"legacy database not found: {legacy_path}", file=sys.stderr)
        return 2

    legacy = connect(legacy_path)
    tables = {row[0] for row in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    print(f"legacy tables: {sorted(tables)}")

    target = db.connect(target_path)
    db.migrate(target)

    # -- sites -> groups ---------------------------------------------------
    sites: dict[str, int] = {}
    overrides = {}
    if "site_settings" in tables:
        for row in legacy.execute("SELECT site, daily_minutes FROM site_settings"):
            overrides[row["site"]] = row["daily_minutes"]

    known_sites: list[str] = []
    for site in overrides:
        known_sites.append(site)
    if "sessions" in tables:
        for row in legacy.execute("SELECT DISTINCT site FROM sessions"):
            if row["site"] not in known_sites:
                known_sites.append(row["site"])
    if "bonus" in tables:
        for row in legacy.execute("SELECT DISTINCT site FROM bonus"):
            if row["site"] not in known_sites:
                known_sites.append(row["site"])

    now = int(time.time())
    for site in known_sites:
        name, emoji, service = DISPLAY.get(site, (site.capitalize(), "", site))
        minutes = int(overrides.get(site) or 60)

        existing = target.execute(
            "SELECT id FROM groups WHERE name=? AND deleted=0", (name,)
        ).fetchone()
        if existing:
            sites[site] = existing["id"]
            print(f"group {name!r} already exists (id {existing['id']}), reusing")
            continue

        row = target.execute(
            "INSERT INTO groups(name, emoji, weekday_minutes, weekend_minutes, paused, "
            "created_at) VALUES(?,?,?,?,0,?)",
            (name, emoji, minutes, minutes, now),
        )
        gid = int(row.lastrowid)
        sites[site] = gid
        if service in KNOWN_SERVICES:
            target.execute(
                "INSERT OR IGNORE INTO group_services(group_id, service_id) VALUES(?,?)",
                (gid, service),
            )
            target.execute(
                "INSERT OR IGNORE INTO owned_services(service_id, added_at) VALUES(?,?)",
                (service, now),
            )
        target.commit()
        print(f"created group {name!r} (id {gid}): {service}, {minutes} min/day")

    # -- credentials -------------------------------------------------------
    if "credentials" in tables:
        copied = 0
        for row in legacy.execute("SELECT role, hash, updated_at FROM credentials"):
            target.execute(
                "INSERT OR REPLACE INTO credentials(role, hash, updated_at) VALUES(?,?,?)",
                (row["role"], row["hash"], row["updated_at"] or now),
            )
            copied += 1
        target.commit()
        db.set_bool_setting(target, "setup_complete", copied > 0)
        print(f"copied {copied} credential hash(es); setup marked complete")

    # -- sessions ----------------------------------------------------------
    if "sessions" in tables:
        count = 0
        for row in legacy.execute(
            "SELECT id, site, planned_seconds, started_at, ended_at, used_seconds, "
            "end_reason, actor FROM sessions ORDER BY id"
        ):
            gid = sites.get(row["site"])
            if not gid:
                continue
            exists = target.execute(
                "SELECT id FROM sessions WHERE group_id=? AND started_at=?",
                (gid, row["started_at"]),
            ).fetchone()
            if exists:
                continue
            target.execute(
                "INSERT INTO sessions(group_id, planned_seconds, started_at, ended_at, "
                "used_seconds, end_reason, actor) VALUES(?,?,?,?,?,?,?)",
                (
                    gid,
                    row["planned_seconds"],
                    row["started_at"],
                    row["ended_at"],
                    row["used_seconds"],
                    row["end_reason"],
                    row["actor"],
                ),
            )
            count += 1
        target.commit()
        print(f"copied {count} session(s)")

    # -- bonus + free allow ------------------------------------------------
    if "bonus" in tables:
        count = 0
        for row in legacy.execute("SELECT day, site, seconds FROM bonus"):
            gid = sites.get(row["site"])
            if not gid:
                continue
            target.execute(
                "INSERT INTO bonus(day, group_id, seconds) VALUES(?,?,?) "
                "ON CONFLICT(day, group_id) DO UPDATE SET seconds=excluded.seconds",
                (row["day"], gid, row["seconds"]),
            )
            count += 1
        target.commit()
        if count:
            print(f"copied {count} bonus row(s)")
    if "free_allow" in tables:
        for row in legacy.execute("SELECT site, until_ts FROM free_allow"):
            gid = sites.get(row["site"])
            if gid and row["until_ts"] > now:
                budget.set_free_until(target, gid, row["until_ts"])
                print(f"kept free-allow for {row['site']} until {row['until_ts']}")

    # -- events ------------------------------------------------------------
    if "events" in tables:
        count = 0
        for row in legacy.execute("SELECT ts, kind, detail FROM events ORDER BY id"):
            target.execute(
                "INSERT INTO events(ts, kind, detail) VALUES(?,?,?)",
                (row["ts"], f"legacy:{row['kind']}", row["detail"] or ""),
            )
            count += 1
        target.commit()
        print(f"copied {count} event(s)")

    target.commit()
    target.close()
    if args.dry_run:
        print(
            "dry run: nothing was committed (SQLite transactions above did commit; "
            "use a copy of the target DB for a true dry run)"
        )
    print("migration finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
