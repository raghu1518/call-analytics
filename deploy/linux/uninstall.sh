#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"

SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
REMOVE_SYSTEMD="${REMOVE_SYSTEMD:-true}"
PURGE_VENV="${PURGE_VENV:-false}"
PURGE_DATA="${PURGE_DATA:-false}"
PURGE_LOGS="${PURGE_LOGS:-false}"
PURGE_ENV="${PURGE_ENV:-false}"

WEB_UNIT="call-analytics-web.service"
CONNECTOR_UNIT="call-analytics-genesys-connector.service"
AUDIOHOOK_UNIT="call-analytics-genesys-audiohook.service"

log() {
  printf '[deploy/linux] %s\n' "$*"
}

fail() {
  printf '[deploy/linux] ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'USAGE'
Automated uninstaller for Call Analytics on Linux.

Usage:
  ./deploy/linux/uninstall.sh [options]

Options:
  --no-systemd            Skip service stop/remove
  --purge-venv            Delete .venv
  --purge-data            Delete data/ directory
  --purge-logs            Delete log/ directory
  --purge-env             Delete .env
  --systemd-dir <path>    systemd unit directory (default: /etc/systemd/system)
  --help                  Show this help

Environment overrides:
  REMOVE_SYSTEMD, PURGE_VENV, PURGE_DATA, PURGE_LOGS, PURGE_ENV, SYSTEMD_DIR
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-systemd)
      REMOVE_SYSTEMD="false"
      shift
      ;;
    --purge-venv)
      PURGE_VENV="true"
      shift
      ;;
    --purge-data)
      PURGE_DATA="true"
      shift
      ;;
    --purge-logs)
      PURGE_LOGS="true"
      shift
      ;;
    --purge-env)
      PURGE_ENV="true"
      shift
      ;;
    --systemd-dir)
      [[ $# -ge 2 ]] || fail "Missing value for --systemd-dir"
      SYSTEMD_DIR="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      fail "Unknown option: $1"
      ;;
  esac
done

if [[ "${REMOVE_SYSTEMD}" == "true" ]]; then
  command -v systemctl >/dev/null 2>&1 || fail "systemctl not found (use --no-systemd)"
  [[ "${EUID}" -eq 0 ]] || fail "systemd uninstall requires root. Re-run with sudo."

  for unit in "${WEB_UNIT}" "${CONNECTOR_UNIT}" "${AUDIOHOOK_UNIT}"; do
    if systemctl list-unit-files --type service | grep -q "^${unit}"; then
      log "Stopping and disabling ${unit}"
      systemctl stop "${unit}" || true
      systemctl disable "${unit}" || true
    fi
    if [[ -f "${SYSTEMD_DIR}/${unit}" ]]; then
      log "Removing ${SYSTEMD_DIR}/${unit}"
      rm -f "${SYSTEMD_DIR}/${unit}"
    fi
  done

  log "Reloading systemd"
  systemctl daemon-reload
  systemctl reset-failed || true
else
  log "Skipping systemd removal (--no-systemd)"
fi

if [[ "${PURGE_VENV}" == "true" && -d "${REPO_DIR}/.venv" ]]; then
  log "Removing virtual environment"
  rm -rf "${REPO_DIR}/.venv"
fi

if [[ "${PURGE_DATA}" == "true" && -d "${REPO_DIR}/data" ]]; then
  log "Removing data directory"
  rm -rf "${REPO_DIR}/data"
fi

if [[ "${PURGE_LOGS}" == "true" && -d "${REPO_DIR}/log" ]]; then
  log "Removing log directory"
  rm -rf "${REPO_DIR}/log"
fi

if [[ "${PURGE_ENV}" == "true" && -f "${REPO_DIR}/.env" ]]; then
  log "Removing .env"
  rm -f "${REPO_DIR}/.env"
fi

log "Linux uninstall completed."
