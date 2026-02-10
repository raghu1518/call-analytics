#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"

WEB_PORT="${WEB_PORT:-8009}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
INSTALL_SYSTEMD="${INSTALL_SYSTEMD:-true}"
ENABLE_CONNECTOR_SERVICE="${ENABLE_CONNECTOR_SERVICE:-true}"
ENABLE_AUDIOHOOK_SERVICE="${ENABLE_AUDIOHOOK_SERVICE:-true}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
APP_USER="${APP_USER:-${SUDO_USER:-$(id -un)}}"
APP_GROUP="${APP_GROUP:-$(id -gn "${APP_USER}")}"

log() {
  printf '[deploy/linux] %s\n' "$*"
}

fail() {
  printf '[deploy/linux] ERROR: %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

usage() {
  cat <<'USAGE'
Automated installer for Call Analytics on Linux.

Usage:
  ./deploy/linux/install.sh [options]

Options:
  --web-port <port>                Django web port (default: 8009)
  --python <python-bin>            Python binary to use (default: python3)
  --no-systemd                     Skip systemd service installation
  --no-connector-service           Do not install connector systemd unit
  --no-audiohook-service           Do not install audiohook systemd unit
  --systemd-dir <path>             systemd unit directory (default: /etc/systemd/system)
  --app-user <user>                service account user (default: SUDO_USER/current)
  --app-group <group>              service account group (default: user primary group)
  --help                           Show this help

Environment overrides:
  WEB_PORT, PYTHON_BIN, INSTALL_SYSTEMD, ENABLE_CONNECTOR_SERVICE,
  ENABLE_AUDIOHOOK_SERVICE, SYSTEMD_DIR, APP_USER, APP_GROUP
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --web-port)
      [[ $# -ge 2 ]] || fail "Missing value for --web-port"
      WEB_PORT="$2"
      shift 2
      ;;
    --python)
      [[ $# -ge 2 ]] || fail "Missing value for --python"
      PYTHON_BIN="$2"
      shift 2
      ;;
    --no-systemd)
      INSTALL_SYSTEMD="false"
      shift
      ;;
    --no-connector-service)
      ENABLE_CONNECTOR_SERVICE="false"
      shift
      ;;
    --no-audiohook-service)
      ENABLE_AUDIOHOOK_SERVICE="false"
      shift
      ;;
    --systemd-dir)
      [[ $# -ge 2 ]] || fail "Missing value for --systemd-dir"
      SYSTEMD_DIR="$2"
      shift 2
      ;;
    --app-user)
      [[ $# -ge 2 ]] || fail "Missing value for --app-user"
      APP_USER="$2"
      APP_GROUP="$(id -gn "${APP_USER}")"
      shift 2
      ;;
    --app-group)
      [[ $# -ge 2 ]] || fail "Missing value for --app-group"
      APP_GROUP="$2"
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

require_cmd "${PYTHON_BIN}"

log "Repository directory: ${REPO_DIR}"
log "Creating/updating Python virtual environment"
"${PYTHON_BIN}" -m venv "${REPO_DIR}/.venv"

VENV_PYTHON="${REPO_DIR}/.venv/bin/python"
[[ -x "${VENV_PYTHON}" ]] || fail "Virtual environment python missing: ${VENV_PYTHON}"

log "Installing Python dependencies"
"${VENV_PYTHON}" -m pip install --upgrade pip
"${VENV_PYTHON}" -m pip install -r "${REPO_DIR}/requirements.txt"

if [[ ! -f "${REPO_DIR}/.env" ]]; then
  log "Creating .env from .env.example"
  cp "${REPO_DIR}/.env.example" "${REPO_DIR}/.env"
else
  log ".env already exists, keeping current values"
fi

log "Creating required runtime directories"
mkdir -p \
  "${REPO_DIR}/data/uploads" \
  "${REPO_DIR}/data/outputs" \
  "${REPO_DIR}/data/db" \
  "${REPO_DIR}/data/runtime/live_audio" \
  "${REPO_DIR}/log"

log "Running migrations and Django checks"
"${VENV_PYTHON}" "${REPO_DIR}/manage.py" migrate
"${VENV_PYTHON}" "${REPO_DIR}/manage.py" check

install_systemd_unit() {
  local unit_name="$1"
  local exec_start="$2"
  local unit_path="${SYSTEMD_DIR}/${unit_name}"
  cat > "${unit_path}" <<EOF
[Unit]
Description=Call Analytics - ${unit_name}
After=network.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${REPO_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${exec_start}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  log "Installed unit: ${unit_path}"
}

if [[ "${INSTALL_SYSTEMD}" == "true" ]]; then
  require_cmd systemctl
  [[ "${EUID}" -eq 0 ]] || fail "--systemd install requires root. Re-run with sudo."

  WEB_UNIT="call-analytics-web.service"
  CONNECTOR_UNIT="call-analytics-genesys-connector.service"
  AUDIOHOOK_UNIT="call-analytics-genesys-audiohook.service"

  install_systemd_unit \
    "${WEB_UNIT}" \
    "${VENV_PYTHON} ${REPO_DIR}/manage.py runserver 0.0.0.0:${WEB_PORT}"

  if [[ "${ENABLE_CONNECTOR_SERVICE}" == "true" ]]; then
    install_systemd_unit \
      "${CONNECTOR_UNIT}" \
      "${VENV_PYTHON} ${REPO_DIR}/manage.py run_genesys_connector"
  fi

  if [[ "${ENABLE_AUDIOHOOK_SERVICE}" == "true" ]]; then
    install_systemd_unit \
      "${AUDIOHOOK_UNIT}" \
      "${VENV_PYTHON} ${REPO_DIR}/manage.py run_genesys_audiohook_listener"
  fi

  log "Reloading systemd"
  systemctl daemon-reload

  log "Enabling and starting web service"
  systemctl enable --now "${WEB_UNIT}"

  if [[ "${ENABLE_CONNECTOR_SERVICE}" == "true" ]]; then
    log "Enabling and starting Genesys connector service"
    systemctl enable --now "${CONNECTOR_UNIT}"
  fi

  if [[ "${ENABLE_AUDIOHOOK_SERVICE}" == "true" ]]; then
    log "Enabling and starting AudioHook service"
    systemctl enable --now "${AUDIOHOOK_UNIT}"
  fi

  log "Service status (summary):"
  systemctl --no-pager --full status "${WEB_UNIT}" || true
  if [[ "${ENABLE_CONNECTOR_SERVICE}" == "true" ]]; then
    systemctl --no-pager --full status "${CONNECTOR_UNIT}" || true
  fi
  if [[ "${ENABLE_AUDIOHOOK_SERVICE}" == "true" ]]; then
    systemctl --no-pager --full status "${AUDIOHOOK_UNIT}" || true
  fi
else
  log "Skipping systemd setup (--no-systemd)"
fi

cat <<EOF

Linux deployment completed.

Repo: ${REPO_DIR}
Web URL: http://127.0.0.1:${WEB_PORT}

Manual start (no systemd):
  ${VENV_PYTHON} ${REPO_DIR}/manage.py runserver 0.0.0.0:${WEB_PORT}
  ${VENV_PYTHON} ${REPO_DIR}/manage.py run_genesys_connector
  ${VENV_PYTHON} ${REPO_DIR}/manage.py run_genesys_audiohook_listener
EOF
