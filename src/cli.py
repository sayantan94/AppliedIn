"""AppliedIn CLI — the one entrypoint, both modes.

    APPLIEDIN_MODE=local  OPENAI_API_KEY=...  appliedin start   # Mac
    APPLIEDIN_MODE=cloud                          (EventBridge + SQS drive it)

Daemon lifecycle (local):
    start [--no-discover] [--port N] [--fresh]
               Launch the daemon in the background (cron finder + queue worker
               + dashboard). --no-discover turns the crawler off: dashboard +
               queue worker only, for testing the approval flow without crawling.
    stop       Stop the running daemon.
    status     Is the daemon up? + the pipeline board (what's in each state).
    logs       Tail the background daemon's log (Ctrl-C to stop following).

Two instances at once (--port):
    Any port other than 8787 is a SEPARATE instance and shares no data with the
    default one: its own `.local-<port>` directory (board, answer bank, profiles,
    résumés, logins) and its own Redis database, so it cannot see or dispatch the
    other's applications. The port is the instance's identity, so every lifecycle
    command takes it:

        appliedin start  --port 8788 --fresh   # a clean instance, no history
        appliedin status --port 8788
        appliedin stop   --port 8788

    --fresh empties that instance first. It refuses to run against the default
    instance, where it would delete the real board and every tailored résumé.

One-shot commands:
    discover   Find new jobs across the watchlist and enqueue them.
    work       Drain the queue and run the pipeline for each job.
    run        discover, then work.
    resume     Answer a gated job:  appliedin resume <pk> "<answer>"

Portal sign-ins (Apple, Google, Workday and other 2FA walls) need no command:
applies run in your OWN Chrome, so signing in there once in the ordinary way is
all it takes. There used to be an `appliedin login` for exactly this, back when
applies ran in a separate Playwright browser with its own cookie jar. That
browser is gone, and the command outlived it by calling a function deleted with
it, so every invocation raised NameError.

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


DEFAULT_PORT = 8787

# Where a second instance keeps its own everything. Every path and connection in
# the product already derives from three settings — the web port, `local_dir` and
# `redis_url` — so an isolated instance is those three moved together, and nothing
# else in the codebase needs to know instances exist.
#
# The port IS the instance identity, deliberately: `stop --port 8788` has to find
# the same state `start --port 8788` created, and remembering a second flag to
# say which instance you meant is a way to stop the wrong one. So a non-default
# port always implies its own directory and its own Redis database.
#
# Redis is the part that would otherwise leak. The tracking rows, the apply queue
# and the activity feed all live in one database with unprefixed keys, so two
# instances pointed at db 0 would share a board: the new one would list the old
# one's applications and could dispatch them. Each instance gets its own db index
# instead, derived from the port so it is the same one every time.
_REDIS_DBS = 15   # a stock Redis serves 0-15; 0 stays reserved for the default


def _instance_db(port: int) -> int:
    return 1 + (port % _REDIS_DBS)


def _apply_instance(port: int | None, *, fresh: bool = False) -> dict:
    """Point this process at one instance. Returns what it decided, for reporting.

    Called before anything reads settings, and it clears the settings cache so a
    value read earlier cannot outlive the switch.
    """
    chosen = int(port or os.environ.get("APPLIEDIN_WEB_PORT") or DEFAULT_PORT)
    os.environ["APPLIEDIN_WEB_PORT"] = str(chosen)
    out: dict = {"port": chosen, "instance": "default"}
    if chosen != DEFAULT_PORT:
        out["instance"] = f"port-{chosen}"
        # Set UNCONDITIONALLY. An earlier version deferred to an existing
        # APPLIEDIN_LOCAL_DIR "so an explicit value wins" — but .env already sets
        # one, so the isolation silently did nothing while --fresh went on to
        # delete what it believed was a scratch directory. It was the real one.
        # A non-default port IS the instance; nothing inherited may redirect it.
        os.environ["APPLIEDIN_LOCAL_DIR"] = f".local-{chosen}"
        os.environ["APPLIEDIN_REDIS_URL"] = (
            f"redis://localhost:6379/{_instance_db(chosen)}")
    get_settings.cache_clear()
    out["local_dir"] = get_settings().local_dir
    out["redis"] = get_settings().redis_url
    if fresh:
        out["wiped"] = _wipe_instance()
    return out


def _wipe_instance() -> dict:
    """Empty this instance's state so it starts with no history at all.

    Refuses to touch the default instance: `--fresh` on port 8787 would delete
    the real board, the answer bank and every tailored résumé, and no flag should
    be one keystroke away from that.
    """
    import shutil

    port = int(os.environ.get("APPLIEDIN_WEB_PORT", DEFAULT_PORT))
    if port == DEFAULT_PORT:
        return {"refused": "--fresh will not wipe the default instance on port "
                           f"{DEFAULT_PORT}; give it a --port of its own"}
    s = get_settings()
    done: dict = {"local_dir": s.local_dir, "redis": s.redis_url}
    local = Path(s.local_dir)

    # The name has to prove it is this instance's own directory before anything
    # is deleted. Deriving the path and trusting it is what destroyed a real
    # `.local`: every check upstream passed, the path was simply wrong. A delete
    # gets its own check, at the point of deletion, against the one name that
    # could not belong to anybody else.
    expected = f".local-{port}"
    if local.name != expected:
        return {"refused": f"expected this instance's directory to be named "
                           f"{expected!r}, but it resolved to {str(local)!r}. "
                           "Nothing was deleted.",
                "local_dir": str(local)}
    if s.redis_url.rstrip("/").endswith("/0"):
        return {"refused": "this instance resolved to Redis db 0, which is the "
                           "default board. Nothing was deleted.",
                "redis": s.redis_url}
    if local.exists():
        shutil.rmtree(local)
        done["removed"] = str(local)
    local.mkdir(parents=True, exist_ok=True)
    try:
        import redis

        redis.Redis.from_url(s.redis_url).flushdb()
        done["flushed"] = True
    except Exception as exc:  # noqa: BLE001 — a missing Redis is not a reason to stop
        done["flushed"] = False
        done["redis_error"] = str(exc)
    return done


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
    the pid file (e.g. one launched by hand or orphaned from a past session)."""
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


def start(no_discover: bool = False) -> dict:
    """Launch the daemon detached, in the background. With no_discover=True the
    crawler/discovery is off — just the dashboard + queue worker (handy for
    testing the approval workflow without the crawler churning)."""
    if (pid := _live_pid()) is not None:
        return {"status": "already running", "pid": pid, "dashboard": _dashboard(),
                "hint": "use `appliedin stop` first to restart"}

    import subprocess
    import sys

    local = Path(get_settings().local_dir)
    local.mkdir(parents=True, exist_ok=True)
    logfile = local / "daemon.log"
    env = {**os.environ, "APPLIEDIN_MODE": os.environ.get("APPLIEDIN_MODE", "local")}
    if no_discover:
        env["APPLIEDIN_DISCOVERY"] = "off"
    with open(logfile, "a") as fh:
        proc = subprocess.Popen(  # noqa: S603 - our own daemon module
            [sys.executable, "-m", "daemon"],
            stdout=fh, stderr=subprocess.STDOUT,
            start_new_session=True,  # detach so it outlives this CLI
            env=env,
        )
    _pid_file().write_text(str(proc.pid))
    return {"status": "started", "pid": proc.pid, "dashboard": _dashboard(),
            "discovery": "off" if no_discover else "on", "log": str(logfile)}


def stop() -> dict:
    """Stop the running daemon (and its detached process group). Escalates to
    SIGKILL if it lingers — a half-dead worker thread must never keep writing
    state (a zombie run once clobbered a job row minutes after 'stop')."""
    import time

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
    for _ in range(20):  # up to 2s for a graceful exit
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except OSError:
            break  # gone
    else:  # still alive — kill hard
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
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
    st = sub.add_parser("start")
    st.add_argument("--no-discover", action="store_true",
                    help="dashboard + queue worker only, no crawler")
    st.add_argument("--fresh", action="store_true",
                    help="wipe this instance's state first, so it starts with no "
                         "history. Requires a --port of its own; it refuses to "
                         f"wipe the default instance on {DEFAULT_PORT}.")
    # Every lifecycle command takes --port, because the port is how an instance is
    # identified: `stop` without it would stop the default one instead.
    for parser in (st, *[sub.add_parser(n) for n in
                         ("stop", "status", "logs", "discover", "work", "run")]):
        parser.add_argument("--port", type=int, default=None,
                            help=f"web port (default {DEFAULT_PORT}). Any other "
                                 "port is a separate instance with its own .local "
                                 "directory and its own Redis database, so it "
                                 "shares no data with the default one.")
    r = sub.add_parser("resume")
    r.add_argument("pk")
    r.add_argument("answer")

    args = p.parse_args(argv)
    # Switch instances BEFORE any command reads a setting: the pid file, the log,
    # the artifacts and the Redis connection all hang off this choice.
    instance = None
    if getattr(args, "port", None) is not None or getattr(args, "fresh", False):
        instance = _apply_instance(getattr(args, "port", None),
                                   fresh=getattr(args, "fresh", False))
        # `--fresh` with no port of its own would mean "wipe the real board". It
        # is refused rather than partly honoured: starting anyway, having ignored
        # the flag, is how someone ends up believing they are on a clean instance.
        if (instance.get("wiped") or {}).get("refused"):
            print(json.dumps({"status": "refused",
                              "error": instance["wiped"]["refused"],
                              "hint": "appliedin start --port 8788 --fresh"},
                             indent=2))
            return
    if args.cmd == "logs":  # streams to the terminal, not JSON
        logs()
        return
    if args.cmd == "start":
        out = start(no_discover=args.no_discover)
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
    if instance and isinstance(out, dict):
        out["instance"] = instance      # which board this answer is about
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
