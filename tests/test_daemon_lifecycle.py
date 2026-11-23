"""Daemon-PID shutdown regressions use only faked signals, clock and server."""
import signal
import subprocess
import threading
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli
import daemon


def _fake_pidfd(monkeypatch, signals, identity, *, on_open=None, on_signal=None):
    bindings = {}
    closed = []

    def open_pidfd(pid, flags=0):
        assert pid > 1 and flags == 0
        bindings[73] = identity(pid)
        if on_open:
            on_open()
        return 73

    def send(pidfd, sig):
        if on_signal:
            on_signal(sig)
        signals.append((bindings[pidfd], sig))

    monkeypatch.setattr(cli.os, "pidfd_open", open_pidfd, raising=False)
    monkeypatch.setattr(cli.signal, "pidfd_send_signal", send, raising=False)
    monkeypatch.setattr(cli.os, "close", closed.append)
    return closed


class SelfExit(BaseException):
    pass


def test_uvicorn_sigterm_exits_process_even_with_unfinished_executor(monkeypatch):
    # Running the actual Python interpreter's shutdown would join the executor.
    # The supervised Uvicorn signal path must explicitly exit before atexit.
    events = []

    class Config:
        def __init__(self, app, **kwargs):
            events.append(("config", kwargs))

    class Server:
        def __init__(self, config):
            self.config = config

        def handle_exit(self, sig, frame):
            events.append(("signal", sig))

        def run(self):
            self.handle_exit(signal.SIGTERM, None)
            events.append("server-returned")

    monkeypatch.setitem(sys.modules, "uvicorn",
                        SimpleNamespace(Config=Config, Server=Server))
    monkeypatch.setitem(sys.modules, "server",
                        SimpleNamespace(create_app=lambda: object(),
                                        _recover_stuck=lambda settings: events.append("recovered")))
    monkeypatch.setattr("core.config.get_settings", lambda: object())
    monkeypatch.setattr(daemon, "_shutdown_watchdog", lambda *a, **kw: None)

    def exit_process(code):
        events.append(("exit", code))
        raise SelfExit()

    monkeypatch.setattr(daemon.os, "_exit", exit_process)
    stopping = threading.Event()
    with pytest.raises(SelfExit):
        daemon._serve_http(9911, stop_event=stopping)
    assert stopping.is_set()
    assert events[0] == "recovered"
    assert ("signal", signal.SIGTERM) in events
    assert ("exit", 0) in events
    assert events.index("server-returned") < events.index(("exit", 0))


@pytest.mark.parametrize("blocked", [False, True], ids=["idle", "blocked-executor"])
def test_sigterm_deadline_watchdog_bounded_for_idle_and_blocked_work(blocked, monkeypatch):
    waits = []

    class Requested:
        def wait(self, timeout=None):
            waits.append(("requested", timeout))
            return True

    class Finished:
        def wait(self, timeout=None):
            waits.append(("finished", timeout))
            return not blocked

    exits = []

    def fake_exit(code):
        exits.append(code)
        raise SelfExit()

    monkeypatch.setattr(daemon.os, "_exit", fake_exit)
    if blocked:
        with pytest.raises(SelfExit):
            daemon._shutdown_watchdog(Requested(), Finished())
        assert exits == [0]
    else:
        daemon._shutdown_watchdog(Requested(), Finished())
        assert exits == []
    assert waits == [("requested", None), ("finished", daemon._SHUTDOWN_GRACE_S)]
    assert waits[-1][1] < 10.0


_CHILD = r"""
import signal, sys, threading, time, types
sys.path.insert(0, sys.argv[1])
import daemon
import core.config

mode = sys.argv[2]


class Config:
    def __init__(self, app, **kwargs):
        pass


class Server:
    should_exit = False

    def __init__(self, config):
        pass

    def handle_exit(self, sig, frame):
        self.should_exit = True

    def run(self):
        signal.signal(signal.SIGTERM, self.handle_exit)  # as uvicorn does
        print("ready", flush=True)
        while mode == "hung-http" or not self.should_exit:
            time.sleep(0.05)


sys.modules["uvicorn"] = types.SimpleNamespace(Config=Config, Server=Server)
sys.modules["server"] = types.SimpleNamespace(
    create_app=lambda: object(), _recover_stuck=lambda settings: None)
core.config.get_settings = lambda: object()
# An unfinished non-daemon executor-like thread keeps a normal interpreter exit alive.
threading.Thread(target=threading.Event().wait).start()
daemon._serve_http(0)
"""


@pytest.mark.parametrize("mode", ["http-stops", "hung-http"])
def test_real_process_exits_on_sigterm_alone_under_ten_seconds(mode):
    # Real signal delivery to a real interpreter: no SIGKILL, no scope stop.
    src = str(Path(daemon.__file__).resolve().parent)
    child = subprocess.Popen([sys.executable, "-c", _CHILD, src, mode],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        started = time.monotonic()
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=10.0) == 0  # self-exit, not killed by a signal
        assert time.monotonic() - started < 10.0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdout.close()


def test_stop_sigterm_only_succeeds_when_original_pid_disappears(tmp_path, monkeypatch):
    alive = {"value": True}
    signals = []
    monkeypatch.setattr(cli, "_live_pid", lambda: 412)
    monkeypatch.setattr(cli, "_daemon_identity",
                        lambda pid: "start-tick" if alive["value"] else None)
    monkeypatch.setattr(cli, "_pid_file", lambda: tmp_path / "daemon.pid")
    (tmp_path / "daemon.pid").write_text("412")
    closed = _fake_pidfd(
        monkeypatch, signals, lambda _pid: "start-tick",
        on_signal=lambda sig: alive.update(value=False)
        if sig == signal.SIGTERM else None)
    monkeypatch.setattr(cli.os, "kill",
                        lambda *_a: pytest.fail("PID-bound stop must not call os.kill"))

    result = cli.stop()
    assert result == {"status": "stopped", "pid": 412}
    assert signals == [("start-tick", signal.SIGTERM)]
    assert closed == [73]
    assert not (tmp_path / "daemon.pid").exists()


def test_pidfd_pins_original_process_across_pid_reuse_at_signal_time(tmp_path, monkeypatch):
    state = {"identity": "original"}
    signals = []
    monkeypatch.setattr(cli, "_live_pid", lambda: 420)
    monkeypatch.setattr(cli, "_daemon_identity", lambda _pid: state["identity"])
    monkeypatch.setattr(cli, "_pid_file", lambda: tmp_path / "daemon.pid")
    (tmp_path / "daemon.pid").write_text("420")

    def reuse_between_check_and_send(sig):
        if sig == signal.SIGTERM:
            state["identity"] = "reused"

    closed = _fake_pidfd(
        monkeypatch, signals, lambda _pid: state["identity"],
        on_signal=reuse_between_check_and_send)
    monkeypatch.setattr(cli.os, "kill",
                        lambda *_a: pytest.fail("PID-bound stop must not call os.kill"))

    result = cli.stop()
    assert result == {"status": "stopped", "pid": 420}
    # The descriptor was opened for the original process; reuse before the
    # syscall cannot redirect SIGTERM to the new process with the same number.
    assert signals == [("original", signal.SIGTERM)]
    assert closed == [73]


def test_pid_change_after_pidfd_open_is_refused_before_signal(tmp_path, monkeypatch):
    state = {"identity": "original"}
    signals = []
    monkeypatch.setattr(cli, "_live_pid", lambda: 423)
    monkeypatch.setattr(cli, "_daemon_identity", lambda _pid: state["identity"])
    monkeypatch.setattr(cli, "_pid_file", lambda: tmp_path / "daemon.pid")
    (tmp_path / "daemon.pid").write_text("423")

    def reuse_after_open():
        state["identity"] = "reused"

    closed = _fake_pidfd(
        monkeypatch, signals, lambda _pid: state["identity"], on_open=reuse_after_open)
    result = cli.stop()
    assert result == {"status": "refused", "error": "daemon PID changed before SIGTERM"}
    assert signals == []
    assert closed == [73]


def test_stop_fails_closed_when_pidfd_signaling_is_unavailable(monkeypatch):
    monkeypatch.setattr(cli, "_live_pid", lambda: 424)
    monkeypatch.setattr(cli, "_daemon_identity", lambda _pid: "start-tick")
    monkeypatch.delattr(cli.os, "pidfd_open", raising=False)
    monkeypatch.delattr(cli.signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(cli.os, "kill",
                        lambda *_a: pytest.fail("unsafe PID-only fallback is forbidden"))

    assert cli.stop() == {
        "status": "stop failed", "error": "PID-bound signaling is unavailable"}


def test_blocked_daemon_escalates_only_same_pid_after_ten_second_failure(tmp_path, monkeypatch):
    elapsed = {"value": 0.0}
    signals = []
    monkeypatch.setattr(cli, "_live_pid", lambda: 418)
    monkeypatch.setattr(cli, "_daemon_identity", lambda pid: "start-tick")
    monkeypatch.setattr(cli, "_pid_file", lambda: tmp_path / "daemon.pid")
    monkeypatch.setattr(cli.time, "monotonic", lambda: elapsed["value"])
    monkeypatch.setattr(cli.time, "sleep",
                        lambda seconds: elapsed.update(value=elapsed["value"] + seconds))
    closed = _fake_pidfd(monkeypatch, signals, lambda _pid: "start-tick")
    monkeypatch.setattr(cli.os, "kill",
                        lambda *_a: pytest.fail("PID-bound stop must not call os.kill"))
    result = cli.stop()
    assert result["status"] == "timeout" and result["forced_pid_only"]
    assert elapsed["value"] >= 10.0
    assert signals == [("start-tick", signal.SIGTERM), ("start-tick", signal.SIGKILL)]
    assert closed == [73]


def test_reused_pid_cannot_receive_fallback_sigkill(tmp_path, monkeypatch):
    state = {"tick": "original", "now": 0}
    signals = []
    monkeypatch.setattr(cli, "_live_pid", lambda: 425)
    monkeypatch.setattr(cli, "_daemon_identity", lambda pid: state["tick"])
    monkeypatch.setattr(cli, "_pid_file", lambda: tmp_path / "daemon.pid")
    closed = _fake_pidfd(monkeypatch, signals, lambda _pid: state["tick"])
    monkeypatch.setattr(cli.os, "kill",
                        lambda *_a: pytest.fail("PID-bound stop must not call os.kill"))
    monkeypatch.setattr(cli.time, "monotonic", lambda: state["now"])

    def advance(seconds):
        state["now"] += seconds
        state["tick"] = "reused"
    monkeypatch.setattr(cli.time, "sleep", advance)
    result = cli.stop()
    assert result["status"] == "stopped"
    assert signals == [("original", signal.SIGTERM)]
    assert closed == [73]


def test_unrelated_port_listener_cannot_be_stopped(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess(a, 0, "777\n", ""))
    monkeypatch.setattr(cli, "_daemon_identity", lambda pid: None)
    assert cli._port_pid() is None


def test_proc_identity_rejects_wrong_module_checkout_port_and_reused_pid(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    proc = tmp_path / "proc"
    target = proc / "91"
    target.mkdir(parents=True)
    (target / "cmdline").write_bytes(b"/fake/python\0-m\0daemon\0")
    (target / "cwd").symlink_to(root, target_is_directory=True)
    (target / "environ").write_bytes(b"APPLIEDIN_WEB_PORT=8787\0")
    fields = ["S"] + ["0"] * 18 + ["567"] + ["0"]
    (target / "stat").write_text("91 (python) " + " ".join(fields))
    real_path = Path
    monkeypatch.setattr(cli, "Path",
                        lambda value: proc if str(value) == "/proc" else real_path(value))
    monkeypatch.setattr(cli, "_repo_root", lambda: root)
    monkeypatch.delenv("APPLIEDIN_WEB_PORT", raising=False)
    assert cli._daemon_identity(91) == "567"
    (target / "cmdline").write_bytes(b"/fake/python\0-m\0server\0")
    assert cli._daemon_identity(91) is None
    (target / "cmdline").write_bytes(b"/fake/python\0-m\0daemon\0")
    (target / "environ").write_bytes(b"APPLIEDIN_WEB_PORT=8788\0")
    assert cli._daemon_identity(91) is None
    (target / "environ").write_bytes(b"APPLIEDIN_WEB_PORT=8787\0")
    (target / "stat").write_text("91 (python) " + " ".join(fields[:19] + ["890", "0"]))
    assert cli._daemon_identity(91) == "890"


def test_two_same_instance_daemons_cannot_enter_recovery_concurrently(tmp_path, monkeypatch):
    # The second owner is refused before touching the store/recovery/worker
    # boundary. A dead owner releases its kernel lock without queue mutations.
    monkeypatch.setattr("core.config.get_settings",
                        lambda: SimpleNamespace(mode="local", local_dir=str(tmp_path / "synthetic")))
    with daemon._instance_owner():
        with pytest.raises(SystemExit, match="already running"):
            with daemon._instance_owner():
                pytest.fail("duplicate daemon reached startup work")
    with daemon._instance_owner():
        pass


@pytest.mark.parametrize("loop_name", [
    "_discovery_loop", "_worker_loop", "_heartbeat_loop",
])
def test_stopped_worker_loops_do_not_begin_next_cycle(loop_name, monkeypatch):
    stop_event = threading.Event()
    stop_event.set()
    # The already-stopped event must prevent all queued work/discovery/beat;
    # no real store, network or browser may be called.
    if loop_name == "_discovery_loop":
        from core import flags
        monkeypatch.setattr(flags, "get_flag", lambda *a: None)
    elif loop_name == "_worker_loop":
        monkeypatch.setattr(daemon, "make_stores", lambda: SimpleNamespace())
    else:
        from core import flags
        monkeypatch.setattr(flags, "beat_workers", lambda *a: pytest.fail("heartbeat after stop"))
    getattr(daemon, loop_name)(stop_event)


def test_cloud_mode_does_not_acquire_a_local_daemon_lock(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.get_settings",
                        lambda: SimpleNamespace(mode="cloud", local_dir=str(tmp_path / "synthetic")))
    with daemon._instance_owner():
        pass
    assert not (tmp_path / "synthetic").exists()
