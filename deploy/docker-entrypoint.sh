#!/bin/sh
# TimeFence container entrypoint: honours LISTEN_HOST / LISTEN_PORT / LOG_LEVEL
# so the same image can be loopback-only (host networking) or 0.0.0.0 (bridge).
set -eu

host="${LISTEN_HOST:-0.0.0.0}"
port="${LISTEN_PORT:-8099}"
level="$(printf '%s' "${LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')"

exec uvicorn timefence.main:app \
    --host "$host" \
    --port "$port" \
    --no-server-header \
    --no-proxy-headers \
    --log-level "$level"
