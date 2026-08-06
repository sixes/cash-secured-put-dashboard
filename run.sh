#!/usr/bin/env bash
# Dev server control for the CSP screener.
#
#   ./run.sh start | stop | restart | status | log
#
# Binds every interface so other machines can reach it. There is NO authentication:
# anyone who can reach this port can load arbitrary tickers and therefore spend the
# account-wide option quota (500 symbols/min) that every ticker shares. Run with
# HOST=127.0.0.1 to go back to loopback only.

set -euo pipefail

cd "$(dirname "$0")"

HOST="${HOST:-0.0.0.0}"
# 9188 belongs to the long-bridge-fat-premium-options-web backend, which runs
# continuously on this host.
PORT="${PORT:-9288}"

PY=.venv/bin/python
PID_FILE=data/run/uvicorn.pid
LOG_FILE=data/logs/app.log

# Not cosmetic: the longport SDK returns naive datetimes in host-local time, so a
# non-UTC host silently shifts every timestamp it hands back.
export TZ=UTC

running() {
  [[ -f $PID_FILE ]] || return 1
  local pid
  pid=$(<"$PID_FILE")
  [[ -n $pid ]] || return 1
  # Match the command line too: PIDs get recycled, and stop must never signal
  # whatever unrelated process inherited this number.
  grep -qa app.main "/proc/$pid/cmdline" 2>/dev/null
}

start() {
  if running; then
    echo "already running (pid $(<"$PID_FILE")) at $(urls)"
    return 0
  fi

  if [[ ! -x $PY ]]; then
    echo "no interpreter at $PY — create the venv first:" >&2
    echo "  /home/fec/.local/bin/python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    return 1
  fi

  mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"

  nohup "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"

  # Credentials, a bad port and an import error all fail here rather than at the
  # first request, so confirm the process is still alive before claiming success.
  sleep 2
  if running; then
    echo "started (pid $(<"$PID_FILE")) at $(urls)"
    echo "logs: $LOG_FILE"
  else
    rm -f "$PID_FILE"
    echo "failed to start — last 20 log lines:" >&2
    tail -n 20 "$LOG_FILE" >&2
    return 1
  fi
}

stop() {
  if ! running; then
    rm -f "$PID_FILE"
    echo "not running"
    return 0
  fi

  local pid
  pid=$(<"$PID_FILE")
  kill "$pid"

  # SIGTERM lets uvicorn close the Longbridge long link and the open SSE streams.
  for _ in $(seq 1 50); do
    running || break
    sleep 0.2
  done

  if running; then
    echo "pid $pid ignored SIGTERM after 10s; forcing" >&2
    kill -9 "$pid"
    sleep 0.5
  fi

  rm -f "$PID_FILE"
  echo "stopped (pid $pid)"
}

urls() {
  if [[ $HOST != 0.0.0.0 ]]; then
    echo "http://$HOST:$PORT"
    return
  fi
  # 0.0.0.0 is not an address anyone can type, so resolve it to the real ones.
  local lan out="http://127.0.0.1:$PORT"
  lan=$(hostname -I 2>/dev/null | awk '{print $1}')
  [[ -n $lan ]] && out="$out  http://$lan:$PORT"
  echo "$out"
}

status() {
  if ! running; then
    echo "stopped"
    # Non-zero so callers and monitors can branch on it.
    return 1
  fi

  local pid bound
  pid=$(<"$PID_FILE")
  bound=$(ss -ltn "sport = :$PORT" 2>/dev/null | awk '/LISTEN/ {print $4}' | paste -sd', ' -)

  echo "running   pid $pid, up $(ps -o etime= -p "$pid" | tr -d ' ')"
  if [[ -n $bound ]]; then
    echo "listening $bound"
  else
    # Alive but unbound is a real failure mode, not a healthy server.
    echo "listening nothing on $PORT — the process is up but not serving" >&2
  fi
  echo "urls      $(urls)"
  echo "log       $LOG_FILE"
}

log() {
  if [[ ! -f $LOG_FILE ]]; then
    echo "no log yet at $LOG_FILE — start the server first" >&2
    return 1
  fi
  tail -n "${LINES:-50}" -f "$LOG_FILE"
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart)
    stop
    start
    ;;
  log) log ;;
  status) status ;;
  *)
    echo "usage: $0 {start|stop|restart|status|log}" >&2
    exit 2
    ;;
esac
