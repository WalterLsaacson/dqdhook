#!/usr/bin/env bash
# Process wrapper for System Main. Used by systemd and by scripts/dqdhook.sh.
# Cursor-independent: closing the IDE does not stop this process.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONUNBUFFERED=1
export DQD_BIND="${DQD_BIND:-0.0.0.0}"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

# This host reaches Gamma / CLOB / Dongqiudi directly. Do not inherit a leftover
# local 1082 proxy from .env, systemd, or the invoking shell.
export PM_PROXY=none
unset ALL_PROXY all_proxy HTTP_PROXY HTTPS_PROXY http_proxy https_proxy || true

# Pitch-gate overlay: mqtt = Nami MQTT (no Chromium). .env can set dom.
export QUOTE_DQD_STREAM_OBSERVE="${QUOTE_DQD_STREAM_OBSERVE:-1}"
export QUOTE_GATE_SOURCE="${QUOTE_GATE_SOURCE:-mqtt}"

if [[ -z "${DQD_PUBLIC_HOST:-}" ]]; then
  DQD_PUBLIC_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
  export DQD_PUBLIC_HOST
fi

PYTHON="${DQD_PYTHON:-}"
if [[ -z "$PYTHON" && -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
fi
PYTHON="${PYTHON:-python3}"
EXTRA=(--no-browser)

if [[ ! -f "$ROOT/.env" && "${DQD_FORCE_LIVE:-0}" != "1" ]]; then
  echo "dqdhook-run: no .env (PRIVATE_KEY/FUNDER missing) → starting with --no-trade" >&2
  EXTRA+=(--no-trade)
fi

# Optional extra flags, e.g. DQD_EXTRA_ARGS='--goals-mode dry --ft-mode dry'
# shellcheck disable=SC2206
if [[ -n "${DQD_EXTRA_ARGS:-}" ]]; then
  EXTRA+=($DQD_EXTRA_ARGS)
fi

if [[ $# -gt 0 ]]; then
  EXTRA+=("$@")
fi

echo "dqdhook-run: bind=${DQD_BIND} public_host=${DQD_PUBLIC_HOST:-?} extra=${EXTRA[*]}" >&2
exec "$PYTHON" "$ROOT/frontend/run_main.py" "${EXTRA[@]}"
