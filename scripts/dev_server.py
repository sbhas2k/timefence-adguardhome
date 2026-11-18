"""Run TimeFence on 127.0.0.1:8099 against an in-memory fake AdGuard Home.

For manual and accessibility checks only: it never talks to a real AdGuard
instance and its data lives in a temporary directory that is thrown away when
the process exits.

    TZ=Europe/London python scripts/dev_server.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from fakes import FakeAdGuard  # noqa: E402
from timefence.api import create_app  # noqa: E402
from timefence.config import Settings  # noqa: E402

HOST = "127.0.0.1"
PORT = 8099


def build_settings(data_dir: str) -> Settings:
    env = {
        "ADGUARD_URL": "http://127.0.0.1:3000",
        "ADGUARD_USER": "admin",
        "ADGUARD_PASSWORD": "fake",
        "BASE_PATH": "/",
        "TZ": os.environ.get("TZ") or "UTC",
        "DATA_DIR": data_dir,
        "SECURE_COOKIES": "false",
        "RECONCILE_INTERVAL": "30",
        "LOG_LEVEL": os.environ.get("LOG_LEVEL") or "INFO",
    }
    return Settings.from_env(env)


def main() -> None:
    import uvicorn

    data_dir = tempfile.mkdtemp(prefix="timefence-dev-")
    settings = build_settings(data_dir)
    app = create_app(settings, adguard=FakeAdGuard())
    uvicorn.run(app, host=HOST, port=PORT, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
