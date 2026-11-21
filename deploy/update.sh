#!/usr/bin/env bash
# Manual deploy of TimeFence on the host that runs it (no CI/CD needed).
#   sudo -E ./deploy/update.sh            # pull, build, restart, health-check
#   sudo -E ./deploy/update.sh --rollback 0.2.1
#
# Assumes the compose file that runs the app (default /opt/timefence-docker/compose.yaml)
# uses an image tag of the form  image: timefence:<version>.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPOSE_FILE="${COMPOSE_FILE:-/opt/timefence-docker/compose.yaml}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8099/timer/healthz}"

cd "$REPO_DIR"
[ -f "$COMPOSE_FILE" ] || { echo "compose file not found: $COMPOSE_FILE" >&2; exit 1; }

current_tag() { sed -nE 's/^[[:space:]]*image: timefence:([^[:space:]]+).*/\1/p' "$COMPOSE_FILE" | head -1; }

set_tag() {
  local tag="$1"
  sudo sed -i -E "s|^([[:space:]]*image: timefence:)[^[:space:]]+|\1${tag}|" "$COMPOSE_FILE"
}

wait_healthy() {
  for _ in $(seq 1 40); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "$HEALTH_URL" || true)"
    if [ "$code" = "200" ]; then echo "health: OK ($HEALTH_URL)"; return 0; fi
    sleep 1
  done
  echo "health check FAILED ($HEALTH_URL) - see: docker logs timefence" >&2
  return 1
}

if [ "${1:-}" = "--rollback" ] || [ "${1:-}" = "rollback" ]; then
  target="${2:-}"
  [ -n "$target" ] || { echo "usage: $0 --rollback <version>" >&2; exit 2; }
  echo "rolling back to timefence:${target} (previous: $(current_tag))"
  set_tag "$target"
  sudo docker compose -f "$COMPOSE_FILE" up -d
  wait_healthy
  exit 0
fi

before="$(current_tag)"
echo "== git pull =="
sudo git pull --ff-only

version="$(python3 -c 'import tomllib,sys; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')"
echo "== building timefence:${version} (current tag: ${before}) =="
sudo docker build -t "timefence:${version}" .

echo "== switching compose to ${version} =="
set_tag "$version"
sudo docker compose -f "$COMPOSE_FILE" up -d

if wait_healthy; then
  echo "deployed ${version}; rollback with: $0 --rollback ${before}"
else
  echo "keeping ${version} running for inspection; rollback: $0 --rollback ${before}" >&2
  exit 1
fi
