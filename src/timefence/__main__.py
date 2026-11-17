"""`python -m timefence` and the `timefence` console script."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys

from . import __version__


def _prompt_secret(role: str) -> str:
    label = "New admin password" if role == "admin" else "New user password"
    first = getpass.getpass(f"{label}: ")
    second = getpass.getpass(f"Repeat {label.lower()}: ")
    if first != second:
        raise SystemExit("the two values do not match")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="timefence", description="TimeFence for AdGuard Home")
    parser.add_argument("--version", action="version", version=f"timefence {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the web server (default)")
    run.add_argument("--host", default=None, help="override LISTEN_HOST")
    run.add_argument("--port", type=int, default=None, help="override LISTEN_PORT")

    sub.add_parser("check", help="validate configuration and test the AdGuard connection")

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
