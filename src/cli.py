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
import subprocess
import time
from pathlib import Path
from typing import cast

from core.config import get_settings
from core.logging import get_logger
from core.models import Status
from core.stores import make_stores

DEFAULT_PORT = 8787
CONFIGURED_PORT = int(os.environ.get("APPLIEDIN_WEB_PORT") or DEFAULT_PORT)

log = get_logger(__name__)

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
    if chosen != CONFIGURED_PORT:
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
    if port == CONFIGURED_PORT:
        return {"refused": "--fresh will not wipe the default instance on port "
                           f"{CONFIGURED_PORT}; give it a --port of its own"}
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

    if _unit_owner() == "ours":
        try:
            subprocess.run(["journalctl", "--user", "-u", _SERVICE,
                            "-n", "200", "-f"])
        except (OSError, KeyboardInterrupt):
            pass
        return
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



# The installed login unit never runs setup or browser preflight on startup.
_SERVICE = "appliedin.service"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _service_unit() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / (
        "systemd/user/" + _SERVICE)


def _unit_path(value: Path, *, word: bool) -> str:
    raw = str(value)
    if not raw.startswith("/") or any(c in raw for c in ("\n", "\r", "\0")):
        raise ValueError("checkout path cannot be represented in a systemd unit")
    # Percent is a systemd specifier; executable paths are one argument even
    # when the checkout name contains spaces.
    raw = raw.replace("%", "%%").replace("\\", "\\\\")
    if word:
        return '"' + raw.replace('"', '\\"').replace("$", "$$") + '"'
    return raw.replace(" ", "\\x20")


def _render_unit() -> str:
    root = _repo_root()
    template = (root / "systemd/appliedin.service.in").read_text()
    return (template.replace("@WORKDIR@", _unit_path(root, word=False))
            .replace("@PYTHON@", _unit_path(root / ".venv/bin/python", word=True)))


def _default_instance() -> bool:
    return int(os.environ.get("APPLIEDIN_WEB_PORT") or DEFAULT_PORT) == DEFAULT_PORT


def _unit_owner() -> str:
    """Absent, ours, or a unit owned by another checkout/configuration."""
    if not _default_instance():
        return "absent"
    unit = _service_unit()
    if not unit.exists():
        return "foreign" if _systemctl("cat", _SERVICE).returncode == 0 else "absent"
    try:
        return "ours" if unit.read_text() == _render_unit() else "foreign"
    except (OSError, ValueError):
        return "foreign"


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args],
                          capture_output=True, text=True, timeout=12)


def _daemon_identity(pid: int) -> str | None:
    """Proof of checkout, selected instance and original process start.

    A PID file or socket-owner observation alone can refer to a reused/unrelated
    PID. Private proc data is never exposed in diagnostics or public logs.
    """
    if pid <= 1:
        return None
    proc = Path("/proc") / str(pid)
    try:
        argv = [p.decode(errors="replace") for p in
                (proc / "cmdline").read_bytes().split(b"\0") if p]
        if not any(argv[i:i + 2] == ["-m", "daemon"]
                   for i in range(len(argv) - 1)):
            return None
        if (proc / "cwd").resolve(strict=True) != _repo_root():
            return None
        env = (proc / "environ").read_bytes().split(b"\0")
        ports = [v.partition(b"=")[2].decode("ascii") for v in env
                 if v.startswith(b"APPLIEDIN_WEB_PORT=")]
        if len(ports) > 1:
            return None
        observed = ports[0] if ports else str(DEFAULT_PORT)
        selected = str(int(os.environ.get("APPLIEDIN_WEB_PORT") or DEFAULT_PORT))
        if observed != selected:
            return None
        # /proc/stat comm may itself contain parentheses and spaces.
        fields = (proc / "stat").read_text().rsplit(") ", 1)[1].split()
        if fields[0] in ("Z", "X"):
            return None
        return fields[19]  # kernel starttime changes on PID reuse
    except (OSError, ValueError, IndexError, UnicodeDecodeError):
        return None


def _service_pid() -> int | None:
    if _unit_owner() != "ours":
        return None
    try:
        result = _systemctl("show", _SERVICE, "--property=MainPID", "--value")
        if result.returncode:
            return None
        pid = int(result.stdout.strip())
        return pid if _daemon_identity(pid) is not None else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def install_service() -> dict:
    """Idempotent local user-service installation; foreign units are not overwritten."""
    if os.environ.get("APPLIEDIN_MODE", "local") != "local" or not _default_instance():
        return {"status": "refused", "error": "user service is only for the local default instance"}
    if not (_repo_root() / ".venv/bin/python").is_file():
        return {"status": "setup required", "error": "run ./appliedin setup first"}
    owner = _unit_owner()
    if owner == "foreign":
        return {"status": "refused", "error": "existing user unit differs; inspect it manually"}
    try:
        if owner == "absent":
            path = _service_unit()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_render_unit())
        # An existing manually started daemon already owns the instance. Enable
        # login startup, but do not launch a competing unit and trigger a
        # restart-on-failure loop against its instance lock.
        live = _live_pid()
        service_pid = _service_pid()
        manual_owner = live is not None and live != service_pid
        enable = ("enable", _SERVICE) if manual_owner else ("enable", "--now", _SERVICE)
        for args in (("daemon-reload",), enable):
            result = _systemctl(*args)
            if result.returncode:
                return {"status": "service error",
                        "error": "systemctl --user " + " ".join(args) + " failed"}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"status": "service error", "error": type(exc).__name__}
    if manual_owner:
        return {"status": "enabled (existing manual daemon)", "unit": _SERVICE,
                "pid": live, "hint": "Unit enabled for the next login; existing daemon was not stopped."}
    pid = _service_pid()
    return {"status": "running" if pid else "enabled (startup unverified)",
            "unit": _SERVICE, "pid": pid,
            "hint": ("The unit is enabled for future logins." if pid else
                     "Unit enabled, but no verified daemon MainPID was observed; "
                     "inspect systemctl --user status appliedin.service.")}


def _running_pid() -> int | None:
    """A recorded PID only counts if its full identity still matches."""
    f = _pid_file()
    if not f.exists():
        return None
    try:
        pid = int(f.read_text().strip())
        if _daemon_identity(pid) is not None:
            return pid
    except (OSError, ValueError):
        pass
    f.unlink(missing_ok=True)
    return None


def _port_pid() -> int | None:
    """Never adopt an arbitrary port listener."""
    port = os.environ.get("APPLIEDIN_WEB_PORT", str(DEFAULT_PORT))
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5)
        pids = {int(line) for line in out.stdout.splitlines() if line.strip().isdigit()}
        valid = [pid for pid in pids if _daemon_identity(pid) is not None]
        return valid[0] if len(valid) == 1 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _live_pid() -> int | None:
    """Refuse ambiguous owners instead of overwriting a PID or stopping the wrong one."""
    candidates = {pid for pid in (_running_pid(), _service_pid(), _port_pid()) if pid}
    if len(candidates) > 1:
        raise ValueError("multiple matching daemons; inspect the selected port")
    pid = next(iter(candidates), None)
    if pid is not None:
        _pid_file().parent.mkdir(parents=True, exist_ok=True)
        _pid_file().write_text(str(pid))
    return pid


def start(no_discover: bool = False) -> dict:
    """Use the installed default unit, or a legacy local isolated instance."""
    owner = _unit_owner()
    if owner == "foreign":
        return {"status": "refused", "error": "a different user unit is installed; inspect it"}

    if owner == "absent":
        # Legacy start always repairs the selected instance before checking for
        # a running daemon. Existing scanner and --port callers rely on this;
        # the installed service bypasses it entirely, including on login.
        from discovery.career_ops_setup import ensure_checkout
        try:
            ensure_checkout(Path(get_settings().local_dir))
        except Exception as exc:
            return {"status": "setup failed", "error": f"Career Ops: {exc}",
                    "dashboard": _dashboard()}

    try:
        pid = _live_pid()
    except ValueError as exc:
        return {"status": "refused", "error": str(exc)}
    if pid is not None:
        return {"status": "already running", "pid": pid, "dashboard": _dashboard(),
                "hint": "stop the existing daemon before restarting"}

    if owner == "ours":
        if no_discover:
            return {"status": "refused", "error": (
                "an installed default unit cannot silently switch discovery mode; "
                "use --port for a separate no-discover instance")}
        try:
            result = _systemctl("start", _SERVICE)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"status": "service error", "error": type(exc).__name__}
        if result.returncode:
            return {"status": "service error", "error": "systemctl --user start failed"}
        pid = _service_pid()
        return {"status": "started" if pid else "starting", "pid": pid,
                "unit": _SERVICE, "dashboard": _dashboard()}

    from tools.browser_runtime import ensure_started
    ready, problem = ensure_started()
    if not ready:
        log.warning("%s", problem)

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


def _signal_verified(pid: int, original: str, pidfd: int,
                     sig: signal.Signals) -> bool:
    # The pidfd keeps a reuse after this check from redirecting the signal.
    if _daemon_identity(pid) != original:
        return False
    try:
        signal.pidfd_send_signal(pidfd, sig)
        return True
    except ProcessLookupError:
        return False


def _clear_pid_file(pid: int) -> None:
    try:
        f = _pid_file()
        if f.exists() and f.read_text().strip() == str(pid):
            f.unlink()
    except OSError:
        pass


def stop() -> dict:
    """Only signal the verified daemon PID; escalation never counts as success."""
    try:
        pid = _live_pid()
    except ValueError as exc:
        return {"status": "refused", "error": str(exc)}
    if pid is None:
        return {"status": "not running"}
    original = _daemon_identity(pid)
    if original is None:
        return {"status": "refused", "error": "daemon PID identity could not be verified"}
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if not callable(pidfd_open) or not callable(pidfd_send_signal):
        return {"status": "stop failed", "error": "PID-bound signaling is unavailable"}
    try:
        pidfd = cast(int, pidfd_open(pid, 0))
    except OSError as exc:
        return {"status": "stop failed", "error": type(exc).__name__}
    try:
        try:
            signalled = _signal_verified(pid, original, pidfd, signal.SIGTERM)
        except OSError as exc:
            return {"status": "stop failed", "error": type(exc).__name__}
        if not signalled:
            return {"status": "refused", "error": "daemon PID changed before SIGTERM"}
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if _daemon_identity(pid) != original:
                _clear_pid_file(pid)
                return {"status": "stopped", "pid": pid}
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        try:
            forced = _signal_verified(pid, original, pidfd, signal.SIGKILL)
        except OSError:
            forced = False
        _clear_pid_file(pid)
        return {"status": "timeout", "pid": pid, "forced_pid_only": forced,
                "error": "SIGTERM-only deadline failed; PID-bound SIGKILL attempted as cleanup"}
    finally:
        try:
            os.close(pidfd)
        except OSError:
            pass


def status() -> dict:
    """Daemon liveness + the pipeline board (counts per state)."""
    try:
        pid = _live_pid()
        pid_error = None
    except ValueError as exc:
        pid, pid_error = None, str(exc)
    stores = make_stores()
    board = {st.value: n for st in Status if (n := len(stores.tracking.query_status(st)))}
    return {
        "daemon": {"running": pid is not None, "pid": pid, "dashboard": _dashboard(),
                   "error": pid_error, "unit": _SERVICE if _unit_owner() == "ours" else None},
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


def imap_setup() -> dict:
    import getpass
    from pathlib import Path
    from core.config import get_settings
    from core.reply_ingest import secure_secrets_file

    settings = get_settings()
    if settings.mode != "local":
        return {"status": "refused", "error": "IMAP setup is available in local mode only."}
    path = Path(settings.local_dir) / "secrets.json"
    secure_secrets_file(path)
    host = input("IMAP host: ").strip()
    mailbox = input("IMAP mailbox: ").strip()
    username = input("IMAP username: ").strip()
    password = getpass.getpass("IMAP app password: ")
    if not all((host, mailbox, username, password)):
        return {"status": "refused", "error": "All IMAP settings are required."}
    from core.stores import make_stores
    make_stores(settings).secrets.put_json("imap", {"host": host, "mailbox": mailbox,
                                                     "username": username, "password": password})
    secure_secrets_file(path)
    return {"status": "saved", "detail": "Restart the daemon to enable configured polling."}



def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="appliedin", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("start")
    st.add_argument("--no-discover", action="store_true",
                    help="dashboard + queue worker only, no crawler")
    st.add_argument("--fresh", action="store_true",
                    help="wipe this instance's state first, so it starts with no "
                         "history. Requires a --port of its own; it refuses to "
                         f"wipe the default instance on {CONFIGURED_PORT}.")
    # Every lifecycle command takes --port, because the port is how an instance is
    # identified: `stop` without it would stop the default one instead.
    for parser in (st, *[sub.add_parser(n) for n in
                         ("stop", "status", "logs", "discover", "work", "run")]):
        parser.add_argument("--port", type=int, default=None,
                            help=f"web port (default {CONFIGURED_PORT}). Any other "
                                 "port is a separate instance with its own .local "
                                 "directory and its own Redis database, so it "
                                 "shares no data with the default one.")
    r = sub.add_parser("resume")
    r.add_argument("pk")
    r.add_argument("answer")

    sub.add_parser("service-install", help="install and enable the default local user daemon at login")
    sub.add_parser("browser-setup", help="install BrowserSkill manually in a clean Chrome profile")
    sub.add_parser("imap-setup", help="configure optional local IMAP reply ingestion")
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
    if args.cmd == "service-install":
        print(json.dumps(install_service(), indent=2))
        return
    if args.cmd == "browser-setup":
        from tools.browser_profile import browser_setup
        print(json.dumps(browser_setup(), indent=2))
        return
    if args.cmd == "imap-setup":
        print(json.dumps(imap_setup(), indent=2))
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
