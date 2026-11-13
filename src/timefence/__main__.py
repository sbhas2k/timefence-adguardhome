"""`python -m timefence` and the `timefence` console script."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="timefence", description="TimeFence for AdGuard Home")
    parser.add_argument("--version", action="version", version=f"timefence {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the web server (default)")
    run.add_argument("--host", default=None, help="override LISTEN_HOST")
    run.add_argument("--port", type=int, default=None, help="override LISTEN_PORT")

    sub.add_parser("check", help="validate configuration and test the AdGuard connection")

    args = parser.parse_args(argv)
    command = args.command or "run"

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
