#!/usr/bin/env bash
set -euo pipefail

VENV_PATH="${HOME}/.venv"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ_FILE="${SCRIPT_DIR}/requirements.txt"

python3 -m venv --system-site-packages "${VENV_PATH}"
"${VENV_PATH}/bin/pip" install --upgrade pip
"${VENV_PATH}/bin/pip" install -r wheel
"${VENV_PATH}/bin/pip" install -r "${REQ_FILE}"
