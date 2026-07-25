#!/usr/bin/env bash
# Restart AppliedIn WITHOUT leaving zombies — and WITH the workers.
#
# Two things matter here, both learned the hard way:
#
# 1) Start `python -m daemon`, NEVER `python -m server`. The daemon spawns the
#    discovery + evaluate + apply threads and THEN serves the dashboard.
#    `python -m server` is uvicorn only: the board renders, the buttons respond,
#    /stats returns 200 — and nothing is ever discovered, scored, tailored or
#    applied. An earlier version of this script started the bare server, and the
#    pipeline sat dead for two days looking perfectly healthy.
#
# 2) Killing the port listener is not enough: the dashboard's open SSE stream
#    (and in-flight apply threads) block uvicorn's graceful shutdown, so the old
#    process lingers forever running stale code. Kill every matching process.
set -euo pipefail
cd "$(dirname "$0")/.."

# Match both entry points — a stale bare-server process squats the port too.
pids=$(pgrep -f "python -m daemon" || true)
pids="$pids $(pgrep -f "python -m server" || true)"
pids=$(echo $pids)  # collapse whitespace
if [ -n "$pids" ]; then
  kill $pids 2>/dev/null || true
  sleep 3
  for p in $pids; do kill -9 "$p" 2>/dev/null || true; done
fi

nohup .venv/bin/python -m daemon > .local/daemon.log 2>&1 &
for _ in $(seq 1 30); do
  curl -s -o /dev/null -m 2 http://127.0.0.1:8787/ && break
  sleep 1
done

# Health is not "the port answers" — it's "the workers are alive". Ask /stats.
sleep 2
workers=$(curl -s -m 5 http://127.0.0.1:8787/stats \
  | .venv/bin/python -c 'import sys,json; d=json.load(sys.stdin); w=d.get("workers_down"); print("DOWN: "+",".join(w) if w else "alive")' 2>/dev/null || echo "unknown")
echo "daemon up: pid $(pgrep -f 'python -m daemon' | head -1) — workers: ${workers}"
