"""The browser session must not inherit this repo's Claude Code memories.

Claude Code's auto-memory is keyed on the WORKING DIRECTORY, and the browser
subprocess inherited the daemon's cwd — the project root. So every instance, on
every port, loaded the same memory directory. Measured, not assumed:

    cwd = project root  → "do your memories mention a Ramp or OpenAI
                           application cap?"                        YES
    cwd = a scratch dir → same question                             NO

Those memories say things like "As of 2026-07-25 Sayantan is at that cap, so
further OpenAI applies fail at submit". A second instance started fresh on port
8788, with an empty board and no applications of its own, read that and refused
to apply. Same for an identity: a name an earlier run filled a form under is
recalled by a later run that is going out under a different one.

The prompt already argues with this in prose — "a limit you met somewhere else
is not evidence about this employer", "anything you recall from a previous
session described a different application at a different moment". AGENTS.md is
explicit that a guard belongs in code, not in a prompt, and this one could not
be anywhere else: the model cannot decline to have been told something.

So the session runs in the scratch directory it already writes its report to.
Nothing else changes — the task is entirely self-contained in the prompt, and
`--add-dir` had already established that no repo file is reachable.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import tools.claude_chrome as cc

REPO = Path(__file__).resolve().parents[2]


class _Proc:
    """Enough of an asyncio subprocess for _run_task_impl to finish."""

    pid, returncode = 4242, 0

    async def communicate(self):
        return b'{"result":"{\\"outcome\\":\\"applied\\"}"}', b""

    async def wait(self):
        return 0


@pytest.fixture
def spawned(monkeypatch):
    """Capture the kwargs the subprocess would have been launched with."""
    seen = {}

    async def _fake(*cmd, **kw):
        seen.update(cmd=cmd, kw=kw)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake)
    cc._LIVE.clear()
    yield seen
    cc._LIVE.clear()


def _run(spawned):
    asyncio.run(cc._run_task_impl("do the thing", report_key="outcome",
                                  model="", timeout_s=5, allow_dirs=None,
                                  kind="apply"))
    return spawned


def test_the_session_does_not_run_in_the_repo(spawned):
    """The repo's path IS the memory key. Running here is what loaded them."""
    cwd = Path(str(_run(spawned)["kw"]["cwd"])).resolve()

    assert cwd != REPO
    assert REPO not in cwd.parents, f"{cwd} is still inside the project tree"


def test_it_runs_in_the_scratch_directory_it_was_given(spawned):
    """Not just 'somewhere else' — the same directory it writes its report to,
    which is already on --add-dir, so the session can still do its one file job."""
    got = _run(spawned)          # one run: a second would make its own directory
    kw, cmd = got["kw"], got["cmd"]
    cwd = str(kw["cwd"])

    assert Path(cwd).is_dir()
    assert "appliedin_chrome_" in cwd
    assert cwd in cmd, "the scratch dir must still be on --add-dir"


def test_each_run_gets_its_own_directory(spawned):
    """A memory written during one application must not be read by the next.

    Per-run isolation is what makes this hold for two instances as well: they
    cannot collide on a path that neither of them reuses.
    """
    first = str(_run(spawned)["kw"]["cwd"])
    second = str(_run(spawned)["kw"]["cwd"])

    assert first != second
