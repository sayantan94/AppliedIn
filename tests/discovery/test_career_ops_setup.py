"""Startup installs once, recovers failed downloads, and never erases local edits."""

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from discovery import career_ops_setup as setup


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    repo = tmp_path / "upstream"
    repo.mkdir()
    setup._git(repo, "init", "--quiet")
    setup._git(repo, "config", "user.name", "Setup test")
    setup._git(repo, "config", "user.email", "setup@example.test")
    setup._git(repo, "config", "commit.gpgsign", "false")
    setup._git(repo, "config", "core.hooksPath", "/dev/null")
    (repo / "provider.txt").write_text("pinned provider")
    setup._git(repo, "add", ".")
    setup._git(repo, "commit", "--quiet", "-m", "Pinned provider")
    revision = setup._git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(setup, "REVISION", revision)
    monkeypatch.setattr(setup, "UPSTREAM", str(repo))
    monkeypatch.setattr(setup, "_validate", Mock())
    return repo, revision


def test_first_install_uses_pin_and_second_start_needs_no_network(tmp_path, monkeypatch, upstream):
    _, revision = upstream
    local = tmp_path / "data with spaces"
    assert setup.ensure_checkout(local)
    installed = local / "integrations/career-ops"
    assert setup._git(installed, "rev-parse", "HEAD") == revision
    monkeypatch.setattr(setup, "UPSTREAM", "unreachable")
    assert not setup.ensure_checkout(local)
    assert (installed / "provider.txt").read_text() == "pinned provider"


def test_failed_download_leaves_no_partial_install_and_can_retry(tmp_path, monkeypatch, upstream):
    repo, _ = upstream
    local = tmp_path / "data"
    monkeypatch.setattr(setup, "UPSTREAM", str(tmp_path / "missing-remote"))
    with pytest.raises(subprocess.CalledProcessError):
        setup.ensure_checkout(local)
    assert not (local / "integrations/career-ops").exists()
    assert not list((local / "integrations").glob(".career-ops-*/*"))
    monkeypatch.setattr(setup, "UPSTREAM", str(repo))
    assert setup.ensure_checkout(local)


def test_dirty_checkout_is_preserved(tmp_path, upstream):
    local = tmp_path / "data"
    setup.ensure_checkout(local)
    edited = local / "integrations/career-ops/provider.txt"
    edited.write_text("local improvements")
    with pytest.raises(ValueError, match="local edits"):
        setup.ensure_checkout(local)
    assert edited.read_text() == "local improvements"


def test_clean_checkout_updates_to_new_supported_pin(tmp_path, monkeypatch, upstream):
    repo, _ = upstream
    local = tmp_path / "data"
    setup.ensure_checkout(local)
    (repo / "provider.txt").write_text("updated provider")
    setup._git(repo, "commit", "-am", "Next supported version")
    revision = setup._git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(setup, "REVISION", revision)
    assert setup.ensure_checkout(local)
    assert setup._git(local / "integrations/career-ops", "rev-parse", "HEAD") == revision


def test_provider_validation_failure_does_not_publish_install(tmp_path, monkeypatch, upstream):
    monkeypatch.setattr(setup, "_validate", Mock(side_effect=ValueError("Missing module")))
    local = tmp_path / "data"
    with pytest.raises(ValueError, match="Missing module"):
        setup.ensure_checkout(local)
    assert not (local / "integrations/career-ops").exists()


def test_existing_non_repository_files_are_never_replaced(tmp_path, upstream):
    local = tmp_path / "data"
    repo = local / "integrations/career-ops"
    repo.mkdir(parents=True)
    (repo / "notes.txt").write_text("keep me")
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        setup.ensure_checkout(local)
    assert (repo / "notes.txt").read_text() == "keep me"


def test_local_data_directory_matches_dotenv_and_environment_precedence(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    (tmp_path / ".env").write_text('APPLIEDIN_LOCAL_DIR="private data"\n')
    monkeypatch.delenv("APPLIEDIN_LOCAL_DIR", raising=False)
    install = Mock(return_value=False)
    monkeypatch.setattr(setup, "ensure_checkout", install)
    assert setup.main() == 0
    install.assert_called_with(Path("private data"))
    monkeypatch.setenv("APPLIEDIN_LOCAL_DIR", "override")
    assert setup.main() == 0
    install.assert_called_with(Path("override"))


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


@pytest.mark.parametrize("old_node", [True, False])
def test_shell_installs_missing_or_old_node_and_passes_path_to_daemon(tmp_path, old_node):
    # Fake Homebrew and Python keep this test entirely offline and installation-free.
    bin_dir, brew_node = tmp_path / "bin", tmp_path / "brew-node"
    executable(bin_dir / "git", "exit 0\n")
    if old_node:
        executable(bin_dir / "node", "exit 1\n")
    executable(brew_node / "bin/node", "exit 0\n")
    executable(
        bin_dir / "brew",
        'printf "%s\\n" "$*" >> "$TEST_BREW_LOG"\n'
        'if [ "$1" = --prefix ]; then printf "%s\\n" "$TEST_NODE_PREFIX"; fi\n',
    )
    executable(tmp_path / ".venv/bin/python", 'printf "installer %s\\n" "$*"\ncommand -v node\n')
    helper = setup.ROOT / "scripts/integrations/career-ops-setup.sh"
    result = subprocess.run(
        [
            shutil.which("bash"),
            "-c",
            'say() { :; }; warn() { echo "$1"; }; source "$1"; '
            "ensure_career_ops || exit $?; command -v node",
            "test",
            str(helper),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": str(bin_dir),
            "TEST_BREW_LOG": str(tmp_path / "brew.log"),
            "TEST_NODE_PREFIX": str(brew_node),
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert (tmp_path / "brew.log").read_text().splitlines() == ["install node", "--prefix node"]
    assert "installer -m discovery.career_ops_setup" in result.stdout
    assert result.stdout.splitlines()[-1] == str(brew_node / "bin/node")
