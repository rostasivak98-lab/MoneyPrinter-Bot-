#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

kill_port () {
  local PORT="$1"
  local PIDS
  PIDS="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$PIDS" ]; then
    echo "Killing port $PORT pids: $PIDS"
    kill -TERM $PIDS 2>/dev/null || true
    sleep 0.5
    PIDS="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
    [ -z "$PIDS" ] || kill -KILL $PIDS 2>/dev/null || true
  fi
}

pkill -f "python3 csv_recorder.py" 2>/dev/null || true
kill_port 8001
kill_port 8000

echo "Stopped recorder + ports 8001/8000 ✅"
