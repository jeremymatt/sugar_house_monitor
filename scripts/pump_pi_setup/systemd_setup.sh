#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: systemd_setup.sh -on | -off | -full_off

  -on   Install/update unit files and enable auto-restart (production mode)
  -off  Stop pump stack, keep ADC + watchdog running for hardware re-enable
  -full_off  Stop all services (maintenance mode)
USAGE
}

if [[ ${1:-} != "-on" && ${1:-} != "-off" && ${1:-} != "-full_off" ]]; then
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

SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-${USER}}}"
USER_HOME="$(getent passwd "${SERVICE_USER}" | cut -d: -f6)"
if [[ -z "${USER_HOME}" ]]; then
  USER_HOME="/home/${SERVICE_USER}"
fi
SERVICE_GROUP="$(id -gn "${SERVICE_USER}" 2>/dev/null || echo "${SERVICE_USER}")"

VENV_PATH="${USER_HOME}/.venv"
LOG_PATH="${USER_HOME}/pump_controller.log"
ADC_SERVICE="sugar-adc.service"
WATCHDOG_SERVICE="sugar-adc-watchdog.service"
PUMP_SERVICES=("sugar-pump-controller.service" "sugar-vacuum.service" "sugar-uploader.service")

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

install_units() {
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
}

wait_inactive() {
  local retries=25
  local delay=0.2
  local unit
  for unit in "$@"; do
    for ((i=0; i<retries; i++)); do
      if ! systemctl is-active --quiet "${unit}"; then
        break
      fi
      sleep "${delay}"
    done
    if systemctl is-active --quiet "${unit}"; then
      echo "Timed out waiting for ${unit} to stop" >&2
      return 1
    fi
  done
}

wait_active() {
  local retries=25
  local delay=0.2
  local unit
  for unit in "$@"; do
    for ((i=0; i<retries; i++)); do
      if systemctl is-active --quiet "${unit}"; then
        break
      fi
      sleep "${delay}"
    done
    if ! systemctl is-active --quiet "${unit}"; then
      echo "Timed out waiting for ${unit} to start" >&2
      return 1
    fi
  done
}

if [[ "$1" == "-on" ]]; then
  install_units
  systemctl disable --now "${WATCHDOG_SERVICE}" || true
  wait_inactive "${WATCHDOG_SERVICE}"
  systemctl enable --now sugar-pump.target
  echo "Enabled sugar-pump.target (logs -> ${LOG_PATH})"
elif [[ "$1" == "-off" ]]; then
  install_units
  systemctl disable --now sugar-pump.target || true
  systemctl stop "${PUMP_SERVICES[@]}" || true
  systemctl reset-failed "${PUMP_SERVICES[@]}" "${WATCHDOG_SERVICE}" sugar-pump.target || true
  wait_inactive "${PUMP_SERVICES[@]}"
  systemctl enable --now "${ADC_SERVICE}"
  systemctl enable --now "${WATCHDOG_SERVICE}"
  wait_active "${WATCHDOG_SERVICE}"
  echo "Disabled sugar-pump.target; watchdog enabled"
else
  systemctl disable --now sugar-pump.target || true
  systemctl disable --now "${WATCHDOG_SERVICE}" || true
  systemctl disable --now "${ADC_SERVICE}" || true
  systemctl stop "${PUMP_SERVICES[@]}" || true
  systemctl reset-failed "${ADC_SERVICE}" "${WATCHDOG_SERVICE}" "${PUMP_SERVICES[@]}" sugar-pump.target || true
  echo "Disabled sugar-pump.target and stopped all services"
fi
