"""ASGI entry point: `uvicorn timefence.main:app`."""

from __future__ import annotations

import logging

from .api import create_app
from .config import Settings

settings = Settings.from_env()
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
app = create_app(settings)
