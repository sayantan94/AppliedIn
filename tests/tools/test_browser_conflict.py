"""A tab that cannot be driven is not a job that cannot be filled.

Some pages carry an embed that an ad blocker replaces with a page of its OWN
extension. Chrome then refuses to let any other extension act on that tab, and
the failure is deceptive: page-READING tools keep working and show the posting,
while every INTERACTION tool fails with "Cannot access a chrome-extension:// URL
of different extension". The agent reports a posting it could see but could not
click, and a perfectly good application gets recorded as failed for something
that was never about the job. See site-quirks/oracle.md.

These pin that a fault is told apart from a real failure, that the two layers
which pass the message between them still agree, and that sessions run
concurrently rather than queueing behind each other.
"""

import asyncio
import threading

from tools.claude_chrome import _is_browser_conflict


def test_sessions_are_not_serialised():
    """Concurrency is deliberate, and this pins the decision.

    A semaphore briefly serialised every Chrome session, on the theory that two
    sessions on one browser corrupt each other. The theory was wrong — the
    failures that prompted it happened with a SINGLE session running, and two
    launched together both complete. Serialising cost real throughput, because a
    job read would queue behind an apply allowed to run for 45 minutes. If a lock
    ever comes back here, it needs evidence first.
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

    real, cc._run_task_impl = cc._run_task_impl, fake_session
    try:
        threads = [threading.Thread(target=lambda: asyncio.run(
            cc.run_task("t", report_key="k"))) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        cc._run_task_impl = real

    assert concurrent, "the fake session never ran"
    assert max(concurrent) > 1, "sessions are being serialised again"


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
