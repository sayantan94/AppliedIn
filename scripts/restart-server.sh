#!/usr/bin/env bash
# Restart the AppliedIn local server WITHOUT leaving zombies. Killing only the
# port listener is not enough: the dashboard's open SSE stream (and in-flight
# apply threads) block uvicorn's graceful shutdown, so the old process lingers
# forever running stale code. Kill every `python -m server`, then start one.
set -euo pipefail
cd "$(dirname "$0")/.."
pids=$(pgrep -f "python -m server" || true)
if [ -n "$pids" ]; then
  kill $pids 2>/dev/null || true
  sleep 3
  for p in $pids; do kill -9 "$p" 2>/dev/null || true; done
fi
nohup .venv/bin/python -m server > .local/server.restart.log 2>&1 &
for _ in $(seq 1 30); do
  curl -s -o /dev/null -m 2 http://127.0.0.1:8787/ && break
  sleep 1
done
echo "server up: pid $(lsof -iTCP:8787 -sTCP:LISTEN -t) — $(pgrep -cf 'python -m server') process(es) total"
