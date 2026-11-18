"""`python -m timefence` and the `timefence` console script."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from pathlib import Path

from . import __version__


def _prompt_secret(role: str) -> str:
    label = "New admin password" if role == "admin" else "New user password"
    first = getpass.getpass(f"{label}: ")
    second = getpass.getpass(f"Repeat {label.lower()}: ")
    if first != second:
        raise SystemExit("the two values do not match")
    return first


def _read_env_file(path: str) -> dict[str, str]:
    """Parse KEY=VALUE lines; no shell evaluation, one matching quote pair is stripped."""
    values: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _release(args) -> int:
    """Lift TimeFence's blocks in AdGuard Home; foreign settings survive."""
    from . import db
    from .adguard import AdGuardClient, AdGuardError
    from .config import ConfigError, Settings
    from .reconciler import Reconciler

    env = dict(os.environ)
    if args.env_file:
        try:
            env.update(_read_env_file(args.env_file))
        except OSError as exc:
            print(f"error: cannot read {args.env_file}: {exc}", file=sys.stderr)
            return 2
    try:
        settings = Settings.from_env(env)
        settings.validate()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if not settings.db_path.exists():
        print(f"database not found: {settings.db_path}", file=sys.stderr)
        return 2
    client = AdGuardClient(
        settings.adguard_url,
        settings.adguard_user,
        settings.adguard_password,
        timeout=3.0,
    )
    store = db.Store(settings.db_path)
    try:
        removed = Reconciler(store, client, settings.zone).release()
    except AdGuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    if removed["services"]:
        print("unblocked: " + ", ".join(removed["services"]))
    if removed["rules_removed"]:
        print(f"removed {removed['rules_removed']} managed rule line(s)")
    if not removed["services"] and not removed["rules_removed"]:
        print("nothing to remove")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="timefence", description="TimeFence for AdGuard Home")
    parser.add_argument("--version", action="version", version=f"timefence {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the web server (default)")
    run.add_argument("--host", default=None, help="override LISTEN_HOST")
    run.add_argument("--port", type=int, default=None, help="override LISTEN_PORT")

    sub.add_parser("check", help="validate configuration and test the AdGuard connection")

    release = sub.add_parser(
        "release",
        help="lift TimeFence's blocks in AdGuard Home",
        description=(
            "Stop TimeFence first; a running service re-applies its blocks " "within one interval."
        ),
    )
    release.add_argument("--env-file", default=None, help="read settings from this KEY=VALUE file")

    passwd = sub.add_parser(
        "passwd",
        help="set an account password (works while the service runs)",
    )
    passwd.add_argument("--username", default="admin", help="account to update (default admin)")
    passwd.add_argument("--role", choices=("admin", "user"), default="admin")
    passwd.add_argument(
        "--data-dir",
        default=os.environ.get("DATA_DIR", "/var/lib/timefence"),
        help="directory holding timefence.db (default: $DATA_DIR or /var/lib/timefence)",
    )

    args = parser.parse_args(argv)
    command = args.command or "run"

    if command == "passwd":
        from . import credentials

        try:
            secret = _prompt_secret(args.role)
            path = credentials.reset(args.data_dir, args.role, secret, username=args.username)
        except credentials.CredentialError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"password for {args.username!r} ({args.role}) updated in {path}")
        print("Sign in with the new password; existing sessions stay valid.")
        return 0

    if command == "release":
        return _release(args)

    from .config import ConfigError, Settings

    try:
        settings = Settings.from_env()
        settings.validate()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if command == "check":
        logging.basicConfig(level=settings.log_level)
        from .adguard import AdGuardClient

        client = AdGuardClient(
            settings.adguard_url, settings.adguard_user, settings.adguard_password
        )
        ok, detail = client.test_connection()
        print(("OK: " if ok else "FAIL: ") + detail)
        print("config:", settings.redacted())
        return 0 if ok else 1

    import uvicorn

    host = args.host or settings.listen_host
    port = args.port or settings.listen_port
    uvicorn.run("timefence.main:app", host=host, port=port, log_level=settings.log_level.lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
