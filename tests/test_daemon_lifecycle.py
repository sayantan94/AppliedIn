"""Daemon-PID shutdown regressions use only faked signals, clock and server."""
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli
import daemon


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
    with pytest.raises(SelfExit):
        daemon._serve_http(9911)
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
    assert waits == [("requested", None), ("finished", 8.0)]
    assert waits[-1][1] < 10.0


def test_stop_sigterm_only_succeeds_when_original_pid_disappears(tmp_path, monkeypatch):
    alive = {"value": True}
    signals = []
    monkeypatch.setattr(cli, "_live_pid", lambda: 412)
    monkeypatch.setattr(cli, "_daemon_identity",
                        lambda pid: "start-tick" if alive["value"] else None)
    monkeypatch.setattr(cli, "_pid_file", lambda: tmp_path / "daemon.pid")
    (tmp_path / "daemon.pid").write_text("412")

    def fake_kill(pid, sig):
        signals.append((pid, sig))
        if sig == signal.SIGTERM:
            alive["value"] = False

    monkeypatch.setattr(cli.os, "kill", fake_kill)
    result = cli.stop()
    assert result == {"status": "stopped", "pid": 412}
    assert signals == [(412, signal.SIGTERM)]
    assert not (tmp_path / "daemon.pid").exists()


def test_blocked_daemon_escalates_only_same_pid_after_ten_second_failure(tmp_path, monkeypatch):
    elapsed = {"value": 0.0}
    signals = []
    monkeypatch.setattr(cli, "_live_pid", lambda: 418)
    monkeypatch.setattr(cli, "_daemon_identity", lambda pid: "start-tick")
    monkeypatch.setattr(cli, "_pid_file", lambda: tmp_path / "daemon.pid")
    monkeypatch.setattr(cli.time, "monotonic", lambda: elapsed["value"])
    monkeypatch.setattr(cli.time, "sleep",
                        lambda seconds: elapsed.update(value=elapsed["value"] + seconds))
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    result = cli.stop()
    assert result["status"] == "timeout" and result["forced_pid_only"]
    assert elapsed["value"] >= 10.0
    assert signals == [(418, signal.SIGTERM), (418, signal.SIGKILL)]


def test_reused_pid_cannot_receive_fallback_sigkill(tmp_path, monkeypatch):
    state = {"tick": "original", "now": 0}
    signals = []
    monkeypatch.setattr(cli, "_live_pid", lambda: 425)
    monkeypatch.setattr(cli, "_daemon_identity", lambda pid: state["tick"])
    monkeypatch.setattr(cli, "_pid_file", lambda: tmp_path / "daemon.pid")
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(cli.time, "monotonic", lambda: state["now"])

    def advance(seconds):
        state["now"] += seconds
        state["tick"] = "reused"
    monkeypatch.setattr(cli.time, "sleep", advance)
    result = cli.stop()
    assert result["status"] == "stopped"
    assert signals == [(425, signal.SIGTERM)]


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
