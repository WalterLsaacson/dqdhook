#!/usr/bin/env bash
# Install / control dqdhook System Main outside Cursor.
#
#   ./scripts/dqdhook.sh deps     # pip + playwright chromium
#   ./scripts/dqdhook.sh install  # systemd unit + enable on boot
#   ./scripts/dqdhook.sh start | stop | restart | status | logs
#   ./scripts/dqdhook.sh uninstall
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_NAME="dqdhook"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}.service"
RUNNER="$ROOT/scripts/dqdhook-run.sh"
LOG_DIR="$ROOT/data/run"
PID_FILE="$LOG_DIR/dqdhook.pid"
NOHUP_LOG="$LOG_DIR/dqdhook.nohup.log"

resolve_python() {
  if [[ -n "${DQD_PYTHON:-}" ]]; then
    echo "$DQD_PYTHON"
    return
  fi
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    echo "$ROOT/.venv/bin/python"
    return
  fi
  echo python3
}

PYTHON="$(resolve_python)"

usage() {
  cat <<EOF
Usage: $0 <deps|install|uninstall|start|stop|restart|status|logs|urls>

  deps       Install Python trade deps + Playwright Chromium
  install    Install systemd unit and enable on boot (falls back to nohup notes)
  uninstall  Disable and remove the systemd unit
  start      Start the stack (systemd if installed, else nohup)
  stop       Stop the stack
  restart    Restart the stack
  status     Show systemd/nohup status and listening ports
  logs       Follow journal (or nohup log)
  urls       Print public board URLs
EOF
}

have_systemd() {
  [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null 2>&1
}

unit_installed() {
  [[ -f "$UNIT_PATH" ]]
}

public_host() {
  if [[ -n "${DQD_PUBLIC_HOST:-}" ]]; then
    echo "$DQD_PUBLIC_HOST"
    return
  fi
  hostname -I 2>/dev/null | awk '{print $1}'
}

cmd_urls() {
  local host
  host="$(public_host)"
  [[ -n "$host" ]] || host="127.0.0.1"
  cat <<EOF
Hub            http://${host}:8790/
Dongqiudi      http://${host}:8787/
Polymarket     http://${host}:8788/
Match Bridge   http://${host}:8789/
Pitch Gate     http://${host}:8791/
AF Bridge      http://${host}:8792/
Trades Ledger  http://${host}:8790/trades
EOF
}

cmd_deps() {
  echo "Installing Python packages…"
  export PATH="${HOME}/.local/bin:${PATH}"
  if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # shellcheck disable=SC1091
    source "${HOME}/.local/bin/env"
  fi
  # CLOB SDK needs >=3.9.10. Use a repo venv on 3.11 when system python is old.
  if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
    echo "creating $ROOT/.venv with CPython 3.11…"
    uv python install 3.11
    uv venv --python 3.11 "$ROOT/.venv"
  fi
  PYTHON="$ROOT/.venv/bin/python"
  uv pip install --python "$PYTHON" -r "$ROOT/.cursor/skills/polymarket-quote/requirements-trade.txt"
  uv pip install --python "$PYTHON" -r "$ROOT/.cursor/skills/polymarket-soccer/requirements.txt"
  echo "Installing Playwright Chromium (and OS libs if needed)…"
  "$PYTHON" -m playwright install-deps chromium || true
  "$PYTHON" -m playwright install chromium
  echo "deps ok · $($PYTHON -V) @ $PYTHON"
}

write_unit() {
  cat >"$UNIT_PATH" <<EOF
[Unit]
Description=dqdhook System Main (boards + pm_quote watch)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${ROOT}
Environment=PYTHONUNBUFFERED=1
Environment=DQD_BIND=0.0.0.0
Environment=PM_PROXY=none
Environment=DQD_PUBLIC_HOST=$(public_host)
Environment=DQD_PYTHON=${ROOT}/.venv/bin/python
Environment=PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=${RUNNER}
Restart=on-failure
RestartSec=8
TimeoutStopSec=40
KillMode=mixed
StandardOutput=append:${ROOT}/data/run/hub.log
StandardError=append:${ROOT}/data/run/hub.log

[Install]
WantedBy=multi-user.target
EOF
}

cmd_install() {
  chmod +x "$RUNNER" "$ROOT/scripts/dqdhook.sh"
  mkdir -p "$LOG_DIR"
  if have_systemd; then
    write_unit
    systemctl daemon-reload
    systemctl enable "$UNIT_NAME.service"
    echo "installed systemd unit $UNIT_PATH (enabled on boot)"
  else
    echo "systemd not available as PID 1; use: $0 start   (nohup mode)"
  fi
  cmd_urls
}

cmd_uninstall() {
  if have_systemd && unit_installed; then
    systemctl disable --now "$UNIT_NAME.service" || true
    rm -f "$UNIT_PATH"
    systemctl daemon-reload
    echo "removed $UNIT_PATH"
  fi
  if [[ -f "$PID_FILE" ]]; then
    cmd_stop || true
  fi
}

nohup_running() {
  [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

cmd_start() {
  chmod +x "$RUNNER" "$ROOT/scripts/dqdhook.sh"
  if have_systemd && unit_installed; then
    systemctl start "$UNIT_NAME.service"
    systemctl --no-pager --full status "$UNIT_NAME.service" || true
  else
    if nohup_running; then
      echo "already running pid=$(cat "$PID_FILE")"
      return 0
    fi
    mkdir -p "$LOG_DIR"
    nohup "$RUNNER" >>"$NOHUP_LOG" 2>&1 &
    echo $! >"$PID_FILE"
    echo "started nohup pid=$(cat "$PID_FILE") log=$NOHUP_LOG"
  fi
  sleep 1
  cmd_urls
}

cmd_stop() {
  if have_systemd && unit_installed; then
    systemctl stop "$UNIT_NAME.service" || true
    return 0
  fi
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" || true
      sleep 1
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "stopped pid=$pid"
  else
    echo "not running (no pidfile)"
  fi
}

cmd_restart() {
  cmd_stop
  cmd_start
}

cmd_status() {
  if have_systemd && unit_installed; then
    systemctl --no-pager --full status "$UNIT_NAME.service" || true
  elif nohup_running; then
    echo "nohup running pid=$(cat "$PID_FILE")"
  else
    echo "not running"
  fi
  echo
  cmd_urls
  echo
  echo "listening:"
  ss -lntp 2>/dev/null | grep -E ':(8787|8788|8789|8790|8791|8792) ' || true
}

cmd_logs() {
  if have_systemd && unit_installed; then
    journalctl -u "$UNIT_NAME.service" -f
  else
    mkdir -p "$LOG_DIR"
    touch "$NOHUP_LOG"
    tail -f "$NOHUP_LOG"
  fi
}

main() {
  local cmd="${1:-}"
  case "$cmd" in
    deps) cmd_deps ;;
    install) cmd_install ;;
    uninstall) cmd_uninstall ;;
    start) cmd_start ;;
    stop) cmd_stop ;;
    restart) cmd_restart ;;
    status) cmd_status ;;
    logs) cmd_logs ;;
    urls) cmd_urls ;;
    -h|--help|help|"") usage ;;
    *)
      usage
      echo "unknown command: $cmd" >&2
      exit 1
      ;;
  esac
}

main "$@"
