"""AppliedIn CLI — the one entrypoint, both modes.

    APPLIEDIN_MODE=local  ANTHROPIC_API_KEY=...  appliedin start   # Mac
    APPLIEDIN_MODE=cloud                          (EventBridge + SQS drive it)

Daemon lifecycle (local):
    start      Launch the daemon in the background (cron finder + queue worker
               + dashboard) and print the dashboard URL.
    stop       Stop the running daemon.
    status     Is the daemon up? + the pipeline board (what's in each state).
    logs       Tail the background daemon's log (Ctrl-C to stop following).

One-shot commands:
    discover   Find new jobs across the watchlist and enqueue them.
    work       Drain the queue and run the pipeline for each job.
    run        discover, then work.
    resume     Answer a gated job:  appliedin resume <pk> "<answer>"

Cloud uses the same code: discovery runs as a Lambda (cron), and the queue is
consumed by an SQS event source calling agent.run.handler — no CLI loop needed.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger
from core.models import Status
from core.stores import make_stores

log = get_logger(__name__)


def _dashboard() -> str:
    return f"http://127.0.0.1:{os.environ.get('APPLIEDIN_WEB_PORT', '8787')}"


def _pid_file() -> Path:
    return Path(get_settings().local_dir) / "daemon.pid"


def _log_file() -> Path:
    return Path(get_settings().local_dir) / "daemon.log"


def logs() -> None:
    """Follow the background daemon's log (like tail -f). Ctrl-C to stop."""
    import subprocess

    logfile = _log_file()
    if not logfile.exists():
        print(f"no daemon log yet at {logfile} — run `appliedin start` first")
        return
    running = _running_pid()
    print(f"# tailing {logfile} (daemon "
          f"{'running, pid ' + str(running) if running else 'NOT running'}) — Ctrl-C to stop\n")
    try:
        subprocess.run(["tail", "-n", "200", "-f", str(logfile)])  # noqa: S603,S607
    except KeyboardInterrupt:
        pass


def _running_pid() -> int | None:
    """The live daemon PID, or None. Clears a stale pid file."""
    f = _pid_file()
    if not f.exists():
        return None
    try:
        pid = int(f.read_text().strip())
        os.kill(pid, 0)  # signal 0 → just check the process exists
        return pid
    except (ValueError, OSError):
        f.unlink(missing_ok=True)
        return None


def _port_pid() -> int | None:
    """PID of whatever is serving the web port — catches a daemon that isn't in
    the pid file (e.g. one launched by start.sh or orphaned from a past session)."""
    import subprocess

    port = os.environ.get("APPLIEDIN_WEB_PORT", "8787")
    try:
        out = subprocess.run(  # noqa: S603,S607
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return int(out.splitlines()[0]) if out else None
    except Exception:
        return None


def _live_pid() -> int | None:
    """A running daemon by pid file OR by port (adopts an untracked one)."""
    pid = _running_pid() or _port_pid()
    if pid is not None:
        _pid_file().write_text(str(pid))
    return pid


def start() -> dict:
    """Launch the daemon detached, in the background."""
    if (pid := _live_pid()) is not None:
        return {"status": "already running", "pid": pid, "dashboard": _dashboard(),
                "hint": "use `appliedin stop` first to restart"}

    import subprocess
    import sys

    local = Path(get_settings().local_dir)
    local.mkdir(parents=True, exist_ok=True)
    logfile = local / "daemon.log"
    env = {**os.environ, "APPLIEDIN_MODE": os.environ.get("APPLIEDIN_MODE", "local")}
    with open(logfile, "a") as fh:
        proc = subprocess.Popen(  # noqa: S603 - our own daemon module
            [sys.executable, "-m", "daemon"],
            stdout=fh, stderr=subprocess.STDOUT,
            start_new_session=True,  # detach so it outlives this CLI
            env=env,
        )
    _pid_file().write_text(str(proc.pid))
    return {"status": "started", "pid": proc.pid, "dashboard": _dashboard(), "log": str(logfile)}


def stop() -> dict:
    """Stop the running daemon (and its detached process group)."""
    pid = _running_pid() or _port_pid()
    if pid is None:
        return {"status": "not running"}
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)  # the whole detached group
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    _pid_file().unlink(missing_ok=True)
    return {"status": "stopped", "pid": pid}


def status() -> dict:
    """Daemon liveness + the pipeline board (counts per state)."""
    pid = _running_pid()
    stores = make_stores()
    board = {st.value: n for st in Status if (n := len(stores.tracking.query_status(st)))}
    return {
        "daemon": {"running": pid is not None, "pid": pid, "dashboard": _dashboard()},
        "board": board,
    }


def discover() -> dict:
    from pipeline import find

    return find()


def work() -> list[dict]:
    """Drain the queue and run the pipeline per job. Local mode (Redis queue)."""
    from pipeline import apply_queued

    return apply_queued()


def resume(pk: str, answer: str) -> dict:
    from agent.run import resume_job

    return resume_job(pk, answer)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="appliedin", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("start", "stop", "status", "logs", "discover", "work", "run"):
        sub.add_parser(name)
    r = sub.add_parser("resume")
    r.add_argument("pk")
    r.add_argument("answer")

    args = p.parse_args(argv)
    if args.cmd == "logs":  # streams to the terminal, not JSON
        logs()
        return
    if args.cmd == "start":
        out = start()
    elif args.cmd == "stop":
        out = stop()
    elif args.cmd == "status":
        out = status()
    elif args.cmd == "discover":
        out = discover()
    elif args.cmd == "work":
        out = work()
    elif args.cmd == "run":
        from pipeline import run_once

        out = run_once()
    elif args.cmd == "resume":
        out = resume(args.pk, args.answer)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
