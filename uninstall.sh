#!/usr/bin/env bash
# Remove TimeFence. Data is kept unless --purge is given.
set -euo pipefail
PURGE=0
SERVICE_USER="${SERVICE_USER:-timefence}"
INSTALL_DIR="${INSTALL_DIR:-/opt/timefence}"
DATA_DIR="${DATA_DIR:-/var/lib/timefence}"
CONF_DIR="${CONF_DIR:-/etc/timefence}"

for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --help|-h) sed -n '2,4p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || { echo "run as root" >&2; exit 1; }

systemctl disable --now timefence.service 2>/dev/null || true
rm -f /etc/systemd/system/timefence.service
systemctl daemon-reload

rm -rf "${INSTALL_DIR}"

if [[ "${PURGE}" -eq 1 ]]; then
  echo "purging data in ${DATA_DIR} and configuration in ${CONF_DIR}"
  rm -rf "${DATA_DIR}" "${CONF_DIR}"
  id -u "${SERVICE_USER}" >/dev/null 2>&1 && userdel "${SERVICE_USER}" 2>/dev/null || true
  echo "TimeFence removed completely."
else
  echo "TimeFence removed. Kept ${DATA_DIR} and ${CONF_DIR}; re-run with --purge to delete them."
fi
