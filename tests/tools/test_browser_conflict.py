"""One real browser, one session at a time — and a collision is not a job failure.

Two Chrome sessions on the same browser fail in a way that reads as a page
problem: page-READING tools keep working while every INTERACTION tool fails with
"Cannot access a chrome-extension:// URL of different extension". The agent then
reports a posting it could see but could not click, and a perfectly good
application gets recorded as failed for an environment fault.

These pin both halves of the fix: sessions are serialised, and a collision that
slips through anyway is told apart from a real failure.
"""

import asyncio
import threading
import time

from tools.claude_chrome import _is_browser_conflict


def test_only_one_browser_session_runs_at_a_time():
    """The evaluate lanes and the apply lane are separate THREADS with their own
    event loops, so the guard has to be a threading primitive. An asyncio.Lock
    would serialise within a lane and let the lanes collide exactly as before.
    """
    import tools.claude_chrome as cc

    concurrent, live, lock = [], [0], threading.Lock()

    async def fake_session(*_a, **_kw):
        with lock:
            live[0] += 1
            concurrent.append(live[0])
        await asyncio.sleep(0.05)
        with lock:
            live[0] -= 1
        return {}, ""

    real, cc._run_task_locked = cc._run_task_locked, fake_session
    try:
        threads = [threading.Thread(target=lambda: asyncio.run(
            cc.run_task("t", report_key="k"))) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        cc._run_task_locked = real

    assert concurrent, "the fake session never ran"
    assert max(concurrent) == 1, f"{max(concurrent)} sessions shared one browser"


def test_the_lock_is_released_when_a_session_raises():
    """A session that blows up must not keep the browser forever — the next apply
    would wait out the full timeout and report that it never started."""
    import tools.claude_chrome as cc

    async def boom(*_a, **_kw):
        raise RuntimeError("session died")

    real, cc._run_task_locked = cc._run_task_locked, boom
    try:
        try:
            asyncio.run(cc.run_task("t", report_key="k"))
        except RuntimeError:
            pass
    finally:
        cc._run_task_locked = real

    got = cc._BROWSER.acquire(blocking=False)
    if got:
        cc._BROWSER.release()
    assert got, "the browser lock was not released after a failed session"


def test_our_own_conflict_message_is_still_recognised_downstream():
    """The two layers have to agree, and once they did not.

    claude_chrome detects the raw browser error and REWRITES it into readable
    prose. The apply layer then re-reads that prose to decide whether to re-queue
    the job or fail it. The first version of the friendly message contained none
    of the raw markers, so detection worked and the job was still recorded as a
    failed application — the exact outcome the detection existed to prevent.
    """
    from tools.claude_chrome import CONFLICT_MESSAGE

    assert _is_browser_conflict(CONFLICT_MESSAGE)
    # …and still recognised once the apply layer wraps it in its own preamble.
    wrapped = ("The browser agent finished without confirming a submission: "
               + CONFLICT_MESSAGE)
    assert _is_browser_conflict(wrapped)


def test_a_collision_is_recognised_and_a_real_failure_is_not():
    """The distinction decides whether a job is re-queued or burned, so the
    matcher must be tight enough not to swallow genuine failures.
    """
    collisions = [
        "Cannot access a chrome-extension:// URL of different extension",
        "every interaction tool failed with 'Cannot access a chrome-extension://'",
        "Cannot access contents of the page",
    ]
    real_failures = [
        "The form had a required field with no approved answer",
        "Application limit reached for this company",
        "The posting has been taken down",
        "",
    ]
    for text in collisions:
        assert _is_browser_conflict(text), f"missed a collision: {text!r}"
    for text in real_failures:
        assert not _is_browser_conflict(text), f"swallowed a real failure: {text!r}"
