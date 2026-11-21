#!/usr/bin/env bash
# Manual update of a TimeFence checkout on the host that runs it (no CI needed).
#   sudo -E ./deploy/update.sh                    # pull, build, restart, health-check
#   sudo -E ./deploy/update.sh --rollback 1.0.0   # back to a tag built earlier
#
# Run it with sudo -E: the -E keeps BASE_PATH and LISTEN_PORT, which are needed
# to build the health URL. The compose file that runs TimeFence (default
# docker-compose.yml in this checkout) must use an image line of the form
#   image: timefence:<version>
# Environment: COMPOSE_FILE, BASE_PATH, LISTEN_PORT, HEALTH_URL. See
# docs/upgrading.md ("Manual update from a git checkout").
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_DIR/docker-compose.yml}"

base_path="${BASE_PATH:-/}"
base_path="${base_path%/}"
case "$base_path" in
    "" | /*) ;;
    *) base_path="/$base_path" ;;
esac
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:${LISTEN_PORT:-8099}${base_path}/healthz}"

cd "$REPO_DIR" || {
    echo "cannot change to the checkout: $REPO_DIR" >&2
    exit 1
}
[ -f "$COMPOSE_FILE" ] || {
    echo "compose file not found: $COMPOSE_FILE" >&2
    exit 1
}

current_tag() {
    sed -nE 's/^[[:space:]]*image: timefence:([^[:space:]]+).*/\1/p' "$COMPOSE_FILE" | head -1
}

if [ -z "$(current_tag)" ]; then
    echo "the compose file has no 'image: timefence:<tag>' line, so the tag cannot be switched: $COMPOSE_FILE" >&2
    exit 1
fi

check_tag() {
    case "$1" in
        "" | *[!A-Za-z0-9._-]*)
            echo "invalid tag '$1': use only letters, digits, dot, dash and underscore" >&2
            exit 2
            ;;
    esac
}

set_tag() {
    local tag="$1"
    sed -i -E "s|^([[:space:]]*image: timefence:)[^[:space:]]+|\1${tag}|" "$COMPOSE_FILE"
}

read_version() {
    sed -nE 's/^version = "([^"]+)"/\1/p' pyproject.toml | head -1
}

wait_healthy() {
    local code
    for _ in $(seq 1 40); do
        code="$(curl -s -o /dev/null -w '%{http_code}' "$HEALTH_URL" || true)"
        if [ "$code" = "200" ]; then
            echo "health: OK ($HEALTH_URL)"
            return 0
        fi
        sleep 1
    done
    echo "health check failed ($HEALTH_URL): see docker logs timefence" >&2
    return 1
}

if [ "${1:-}" = "--rollback" ] || [ "${1:-}" = "rollback" ]; then
    target="${2:-}"
    [ -n "$target" ] || {
        echo "usage: $0 --rollback <version>" >&2
        exit 2
    }
    check_tag "$target"
    echo "rolling back to timefence:${target} (previous: $(current_tag))"
    set_tag "$target"
    docker compose -f "$COMPOSE_FILE" up -d
    if wait_healthy; then
        echo "rolled back to ${target}"
        exit 0
    fi
    exit 1
fi

before="$(current_tag)"
echo "== git pull =="
if ! git pull --ff-only; then
    echo "git pull --ff-only failed: nothing was built or changed, so resolve the checkout and run again (see docs/upgrading.md)" >&2
    exit 1
fi

version="$(read_version)"
if [ -z "$version" ]; then
    echo "could not read the version from pyproject.toml" >&2
    exit 1
fi
check_tag "$version"

echo "== building timefence:${version} (current tag: ${before}) =="
docker build -t "timefence:${version}" .

echo "== switching compose to ${version} =="
set_tag "$version"
docker compose -f "$COMPOSE_FILE" up -d

if wait_healthy; then
    echo "deployed ${version}; rollback with: $0 --rollback ${before}"
else
    echo "keeping ${version} running for inspection; rollback: $0 --rollback ${before}" >&2
    exit 1
fi
