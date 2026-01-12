#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: systemd_setup.sh -on | -off

  -on   Install/update unit files and enable auto-restart (production mode)
  -off  Stop services and disable auto-restart (testing mode)
USAGE
}

if [[ ${1:-} != "-on" && ${1:-} != "-off" ]]; then
  usage
  exit 1
fi

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
UNIT_SRC_DIR="${SCRIPT_DIR}/systemd"
UNIT_DST_DIR="/etc/systemd/system"
LOGROTATE_SRC_DIR="${SCRIPT_DIR}/logrotate"
LOGROTATE_DST="/etc/logrotate.d/sugar-pump"

SERVICE_USER="${SUDO_USER:-${USER}}"
USER_HOME="$(getent passwd "${SERVICE_USER}" | cut -d: -f6)"
if [[ -z "${USER_HOME}" ]]; then
  USER_HOME="/home/${SERVICE_USER}"
fi
SERVICE_GROUP="$(id -gn "${SERVICE_USER}" 2>/dev/null || echo "${SERVICE_USER}")"

VENV_PATH="${USER_HOME}/.venv"
LOG_PATH="${USER_HOME}/pump_controller.log"

render_unit() {
  local src="$1"
  local dst="$2"
  sed \
    -e "s|__USER__|${SERVICE_USER}|g" \
    -e "s|__GROUP__|${SERVICE_GROUP}|g" \
    -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
    -e "s|__VENV_PATH__|${VENV_PATH}|g" \
    -e "s|__LOG_PATH__|${LOG_PATH}|g" \
    "${src}" > "${dst}"
}

if [[ "$1" == "-on" ]]; then
  if [[ ! -d "${UNIT_SRC_DIR}" ]]; then
    echo "Missing unit template directory: ${UNIT_SRC_DIR}" >&2
    exit 1
  fi
  for unit in "${UNIT_SRC_DIR}"/*.service "${UNIT_SRC_DIR}"/*.target; do
    [[ -e "${unit}" ]] || continue
    dst="${UNIT_DST_DIR}/$(basename "${unit}")"
    render_unit "${unit}" "${dst}"
  done
  if [[ -f "${LOGROTATE_SRC_DIR}/pump_controller" ]]; then
    render_unit "${LOGROTATE_SRC_DIR}/pump_controller" "${LOGROTATE_DST}"
  fi
  systemctl daemon-reload
  systemctl enable --now sugar-pump.target
  echo "Enabled sugar-pump.target (logs -> ${LOG_PATH})"
else
  systemctl disable --now sugar-pump.target || true
  systemctl stop sugar-adc.service sugar-pump-controller.service sugar-vacuum.service sugar-uploader.service || true
  systemctl reset-failed sugar-adc.service sugar-pump-controller.service sugar-vacuum.service sugar-uploader.service sugar-pump.target || true
  echo "Disabled sugar-pump.target"
fi
