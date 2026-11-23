"""Synthetic login-service tests: no systemd, daemon, browser or network."""
from pathlib import Path
import subprocess

import pytest

import cli


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "checkout with spaces"
    (root / "systemd").mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "systemd/appliedin.service.in"
    (root / "systemd/appliedin.service.in").write_text(source.read_text())
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".venv/bin/python").write_text("synthetic executable")
    monkeypatch.setattr(cli, "_repo_root", lambda: root)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPLIEDIN_MODE", "local")
    monkeypatch.delenv("APPLIEDIN_WEB_PORT", raising=False)
    return root


def test_installed_unit_starts_real_module_at_login_not_setup(world):
    template = (world / "systemd/appliedin.service.in").read_text()
    assert "WantedBy=default.target" in template
    assert "ExecStart=@PYTHON@ -m daemon" in template
    assert "WorkingDirectory=@WORKDIR@" in template
    assert "KillMode=process" in template
    assert "Restart=on-failure" in template
    assert "APPLIEDIN_USER_SERVICE=1" in template
    assert "ExecStartPre" not in template
    assert "systemd-run" not in template
    rendered = cli._render_unit()
    assert 'ExecStart="' in rendered
    assert "checkout with spaces" in rendered
    assert "checkout\\x20with\\x20spaces" in rendered
    assert "@WORKDIR@" not in rendered and "@PYTHON@" not in rendered


def test_install_enable_is_idempotent_without_rewriting_existing_unit(world, monkeypatch):
    calls = []

    def systemctl(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "0\n", "")

    monkeypatch.setattr(cli, "_systemctl", systemctl)
    first = cli.install_service()
    unit = cli._service_unit()
    saved, mtime = unit.read_text(), unit.stat().st_mtime_ns
    second = cli.install_service()
    assert first["status"] == second["status"] == "enabled"
    assert unit.read_text() == saved and unit.stat().st_mtime_ns == mtime
    assert calls.count(("daemon-reload",)) == 2
    assert calls.count(("enable", "--now", "appliedin.service")) == 2
    assert cli._unit_owner() == "ours"


def test_missing_setup_and_foreign_unit_fail_without_systemd_side_effect(world, monkeypatch):
    monkeypatch.setattr(cli, "_systemctl",
                        lambda *a: pytest.fail("unexpected systemctl"))
    (world / ".venv/bin/python").unlink()
    assert cli.install_service()["status"] == "setup required"
    (world / ".venv/bin/python").write_text("synthetic")
    unit = cli._service_unit()
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=/other/daemon\n")
    assert cli.install_service()["status"] == "refused"
    assert unit.read_text() == "[Service]\nExecStart=/other/daemon\n"


def test_manual_start_uses_existing_default_unit_not_second_daemon(world, monkeypatch):
    monkeypatch.setattr(cli, "_unit_owner", lambda: "ours")
    monkeypatch.setattr(cli, "_live_pid", lambda: None)
    monkeypatch.setattr(cli, "_service_pid", lambda: 442)
    seen = []
    monkeypatch.setattr(cli, "_systemctl",
                        lambda *args: (seen.append(args) or
                                       subprocess.CompletedProcess(args, 0, "", "")))
    result = cli.start()
    assert result["pid"] == 442 and result["unit"] == "appliedin.service"
    assert seen == [("start", "appliedin.service")]
    assert cli.start(no_discover=True)["status"] == "refused"


def test_running_daemon_prevents_second_service_start(world, monkeypatch):
    monkeypatch.setattr(cli, "_live_pid", lambda: 448)
    monkeypatch.setattr(cli, "_systemctl",
                        lambda *a: pytest.fail("duplicate service start"))
    result = cli.start()
    assert result["status"] == "already running" and result["pid"] == 448


def test_service_not_a_cloud_or_isolated_port_owner(world, monkeypatch):
    monkeypatch.setenv("APPLIEDIN_MODE", "cloud")
    assert cli.install_service()["status"] == "refused"
    monkeypatch.setenv("APPLIEDIN_MODE", "local")
    unit = cli._service_unit()
    unit.parent.mkdir(parents=True)
    unit.write_text(cli._render_unit())
    monkeypatch.setenv("APPLIEDIN_WEB_PORT", "8788")
    assert cli._unit_owner() == "absent"
    assert cli.install_service()["status"] == "refused"
