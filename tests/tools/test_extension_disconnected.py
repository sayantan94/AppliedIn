"""A disconnected Chrome extension halts the queue; it never spends an attempt.

`available()` only checks that the `claude` binary exists, so with the extension
gone the pipeline believes the browser is ready, opens a session, and the
session's tool call comes back with:

    "Browser extension is not connected. Please ensure the Claude browser
     extension is installed and running, and that you are logged into claude.ai
     with the same account as Claude Code."

That string matched nothing in INFRA_OPENINGS, so `retry()` treated it as an
application failure and spent one of the job's three attempts. Three of them
dead-letter a job that never reached a form — the same shape as the expired
login incident the retry docstring memorialises.

It is handled like a signed-out CLI, because it is the same kind of fault: it
does not clear by retrying, only by a person reconnecting the extension. The job
goes back untouched and the board pauses with the reason on it.
"""

from __future__ import annotations

import asyncio
import inspect

import tools.claude_chrome as cc
from core.apply_queue import is_retryable

RAW = ("Browser extension is not connected. Please ensure the Claude browser "
       "extension is installed and running, and that you are logged into claude.ai "
       "with the same account as Claude Code.")


def test_the_tools_own_wording_is_recognised():
    assert cc.is_disconnected(RAW)
    assert cc.is_disconnected(f'{{"error": "{RAW}"}}')


def test_our_rewritten_message_is_recognised_too():
    """The apply layer re-reads the friendly message to decide what to do, so it
    must stay recognisable — the conflict markers learned this the hard way."""
    assert cc.is_disconnected(cc.DISCONNECT_MESSAGE)


def test_it_is_infrastructure_so_no_attempt_is_spent():
    assert cc.is_infrastructure(cc.DISCONNECT_MESSAGE)


def test_an_ordinary_failure_is_not_mistaken_for_it():
    assert not cc.is_disconnected("The form could not be filled: no submit button")
    assert not cc.is_disconnected("")


class _Proc:
    pid, returncode = 4242, 0

    def __init__(self, out: bytes):
        self._out = out

    async def communicate(self):
        return self._out, b""

    async def wait(self):
        return 0


def test_a_session_that_hit_it_reports_the_canonical_message(monkeypatch):
    out = (b'{"type":"result","subtype":"success","num_turns":2,'
           b'"result":"I could not open a tab: ' + RAW.encode() + b'"}')

    async def fake(*cmd, **kw):
        return _Proc(out)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    cc._LIVE.clear()

    report, problem = asyncio.run(cc._run_task_impl(
        "do it", report_key="outcome", model="", timeout_s=5, allow_dirs=None, kind="apply"))

    assert report == {}
    assert problem == cc.DISCONNECT_MESSAGE
    assert is_retryable({"result": "failed", "reason": "unknown"})[0] is True


def test_the_apply_path_pauses_the_board_like_a_signed_out_cli():
    """Pinned by reading the source, the way the dispatch-order test does: the
    branch must sit beside the signed-out one and pause with a named reason."""
    from agent import run as R

    src = inspect.getsource(R._apply_direct)
    assert "is_disconnected(reason)" in src
    i, j = src.index("is_signed_out(reason)"), src.index("is_disconnected(reason)")
    assert abs(i - j) < 2500, "the two faults should be handled together"
    assert 'flags.set_flag("paused_reason"' in src[j:j + 1500]
