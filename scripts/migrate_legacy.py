#!/usr/bin/env python3
"""Import the legacy single-purpose "screentime" database into TimeFence.

The old app (0.0.x) managed exactly one site ("youtube"/"testdom"), stored its
state in /var/lib/screentime/state.db and hashed its credentials with Werkzeug.
TimeFence verifies those hashes natively, so they are copied into the account
records as-is; first-run setup only counts as complete when the imported admin
password is not the default one.

The old app limited each site by the minutes used, and TimeFence now limits by
allowed time windows instead: the old daily minutes and its session history have
no equivalent and are ignored (with a note). Every imported group has no allowed
windows, so it is open until an admin sets times for it.

Usage:
    sudo -u timefence /opt/timefence/venv/bin/python \
        scripts/migrate_legacy.py \
        --legacy /var/lib/screentime/state.db \
        --target /var/lib/timefence/timefence.db \
        --tz Europe/London

--tz is required: it decides which local day each old session belongs to.

Run it before starting the service (or stop the service first); TimeFence then
reconciles AdGuard to the imported state.

`--dry-run` reports what a real run would do and leaves the target database
alone: the import runs against a temporary copy of it.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# a few friendly defaults for the old site names
DISPLAY = {"youtube": ("YouTube", "▶️", "youtube")}
KNOWN_SERVICES = {"youtube", "instagram", "netflix", "tiktok", "roblox", "twitch", "discord"}


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def copy_database(source: Path, destination: Path) -> None:
    """Copy a database (and its WAL) without writing to the original."""
    source = source.resolve()  # as_uri() refuses relative paths
    if not source.exists():
        return  # the dry run starts from an empty database, like a real run would
    origin = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)  # never a writer
    with closing(origin):
        copy = sqlite3.connect(str(destination))
        with closing(copy):
            origin.backup(copy)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", default="/var/lib/screentime/state.db")
    parser.add_argument("--target", default="/var/lib/timefence/timefence.db")
    parser.add_argument(
        "--tz", required=True, help="IANA timezone of the old data, e.g. Europe/London"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would happen; change nothing"
    )
    args = parser.parse_args(argv)

    try:
        tz = ZoneInfo(args.tz)
    except (ZoneInfoNotFoundError, ValueError):
        print(f"unknown timezone: {args.tz}", file=sys.stderr)
        return 2

    legacy_path = Path(args.legacy)
    target_path = Path(args.target)
    if not legacy_path.exists():
        print(f"legacy database not found: {legacy_path}", file=sys.stderr)
        return 2

    legacy = connect(legacy_path)
    workspace: Path | None = None
    try:
        tables = {
            row[0] for row in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        print(f"legacy tables: {sorted(tables)}")

        work_path = target_path
        if args.dry_run:
            workspace = Path(tempfile.mkdtemp(prefix="timefence-dry-run-"))
            work_path = workspace / target_path.name
            copy_database(target_path, work_path)
            print(f"dry run: working on a temporary copy; {target_path} is not modified")
        return _import(legacy, tables, work_path, tz, dry_run=args.dry_run)
    finally:
        legacy.close()
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)


def _import(
    legacy: sqlite3.Connection, tables: set[str], target_path: Path, tz: ZoneInfo, dry_run: bool
) -> int:
    from timefence import budget, credentials, db, security

    with closing(db.connect(target_path)) as target:
        db.migrate(target, tz)

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
        if overrides:
            print(
                f"ignoring the legacy daily minutes for {len(overrides)} site(s): "
                "groups now use allowed time windows, so imported groups are open "
                "until you set times for them"
            )

        for site in known_sites:
            name, emoji, service = DISPLAY.get(site, (site.capitalize(), "", site))

            existing = target.execute(
                "SELECT id FROM groups WHERE name=? AND deleted=0", (name,)
            ).fetchone()
            if existing:
                sites[site] = existing["id"]
                print(f"group {name!r} already exists (id {existing['id']}), reusing")
                continue

            row = target.execute(
                "INSERT INTO groups(name, emoji, paused, created_at) VALUES(?,?,0,?)",
                (name, emoji, now),
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
            print(f"created group {name!r} (id {gid}): {service}")

        # -- accounts ----------------------------------------------------------
        admin_hash = None
        if "credentials" in tables:
            copied = 0
            for row in legacy.execute("SELECT role, hash, updated_at FROM credentials"):
                role = row["role"]
                if role not in ("admin", "user"):
                    continue
                username = "admin" if role == "admin" else "user"
                target.execute(
                    "INSERT INTO users(username, role, hash, created_at, updated_at) "
                    "VALUES(?,?,?,?,?) "
                    "ON CONFLICT(username) DO UPDATE SET hash=excluded.hash, "
                    "updated_at=excluded.updated_at",
                    (
                        username,
                        role,
                        row["hash"],
                        row["updated_at"] or now,
                        row["updated_at"] or now,
                    ),
                )
                if role == "admin":
                    admin_hash = row["hash"]
                copied += 1
            target.commit()
            safe = bool(admin_hash) and not security.verify_secret(
                admin_hash, credentials.DEFAULT_ADMIN_PASSWORD
            )
            db.set_bool_setting(target, "setup_complete", safe)
            state = "complete" if safe else "left open (the admin password is the default)"
            print(f"imported {copied} account hash(es); first-run setup is {state}")

        # -- sessions and bonus ------------------------------------------------
        # The legacy app measured minutes; TimeFence does not meter DNS traffic any
        # more, so there is nothing to carry over.
        if "sessions" in tables or "bonus" in tables:
            print("ignoring the legacy sessions and bonus rows: minutes are no longer metered")
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
    if dry_run:
        print("dry run: only the temporary copy was changed; the target database is untouched")
    print("migration finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
