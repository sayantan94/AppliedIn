"""AppliedIn CLI — the one entrypoint, both modes.

    APPLIEDIN_MODE=local  ANTHROPIC_API_KEY=...  appliedin start   # Mac
    APPLIEDIN_MODE=cloud                          (EventBridge + SQS drive it)

Daemon lifecycle (local):
    start [--no-discover]
               Launch the daemon in the background (cron finder + queue worker
               + dashboard). --no-discover turns the crawler off: dashboard +
               queue worker only, for testing the approval flow without crawling.
    stop       Stop the running daemon.
    status     Is the daemon up? + the pipeline board (what's in each state).
    logs       Tail the background daemon's log (Ctrl-C to stop following).

One-shot commands:
    discover   Find new jobs across the watchlist and enqueue them.
    work       Drain the queue and run the pipeline for each job.
    run        discover, then work.
    resume     Answer a gated job:  appliedin resume <pk> "<answer>"
    login      Sign in to a portal ONCE in the persistent apply profile so every
               future apply reuses the session:  appliedin login [url]
               (for Apple/Google/Workday and other login/2FA walls)

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


def login(url: str = "") -> dict:
    """Open the apply browser's PERSISTENT profile so you can sign in to a portal
    ONCE. The session (cookies) is saved to that profile and every future apply
    reuses it — no stored password, and it survives daemon restarts. Use for
    login/2FA portals (Apple, Google, Workday). Run this while no apply is in
    progress (the profile can only be open in one process at a time)."""
    import asyncio

    s = get_settings()
    profile = (getattr(s, "browser_profile_dir", "") or ".local/chrome-profile").strip()
    channel = (getattr(s, "browser_channel", "") or "chrome").strip() or None
    Path(profile).mkdir(parents=True, exist_ok=True)
    url = url or "https://jobs.apple.com/en-us/search"

    async def _drive() -> dict:
        from playwright.async_api import async_playwright

        root = str(Path(profile).resolve())
        # Strip the automation markers so the ONE-TIME sign-in isn't blocked:
        # Google (and sometimes Apple/Okta) refuse to authenticate a browser that
        # advertises navigator.webdriver / --enable-automation. Applies don't need
        # this (browser-use already strips these), but the login flow does.
        cargs = ["--no-first-run", "--no-default-browser-check",
                 "--disable-blink-features=AutomationControlled"]
        async with async_playwright() as pw:
            ctx = last = None
            for ch in ([channel, None] if channel else [None]):
                try:
                    ctx = await pw.chromium.launch_persistent_context(
                        root, headless=False, channel=ch, args=cargs,
                        ignore_default_args=["--enable-automation"])
                    break
                except Exception as exc:  # noqa: BLE001
                    last = exc
            if ctx is None:
                return {"ok": False, "error": f"couldn't open the profile ({last}). "
                        "Is an apply running? Try `appliedin stop` first, or wait."}
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception:  # noqa: BLE001
                pass
            print(f"\n  ✓ A Chrome window is open at:\n    {url}\n")
            print("  → Sign in fully (username, password, and any 2FA).")
            print(f"    The session is saved to the apply profile ({profile})")
            print("    and reused automatically on every future application.\n")
            await asyncio.get_event_loop().run_in_executor(
                None, input, "  When you're signed in, press Enter here to save & close… ")
            await ctx.close()
        return {"ok": True, "profile": profile, "note": "session saved — future applies reuse it"}

    return asyncio.run(_drive())


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="appliedin", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("start")
    st.add_argument("--no-discover", action="store_true",
                    help="dashboard + queue worker only, no crawler")
    for name in ("stop", "status", "logs", "discover", "work", "run"):
        sub.add_parser(name)
    r = sub.add_parser("resume")
    r.add_argument("pk")
    r.add_argument("answer")
    lg = sub.add_parser("login")
    lg.add_argument("url", nargs="?", default="",
                    help="portal sign-in URL (default: Apple careers)")

    args = p.parse_args(argv)
    if args.cmd == "logs":  # streams to the terminal, not JSON
        logs()
        return
    if args.cmd == "login":  # interactive — opens a browser, waits for you
        out = login(args.url)
        print(json.dumps(out, indent=2, default=str))
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
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
