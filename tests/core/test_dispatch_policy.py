"""Who may take work off the apply queue.

The tailor now queues every finished job, which is the point of having a queue.
That makes this the load-bearing check: if the worker drained the queue in gated
mode, tailoring a job would apply to it, and the rule that an application only
goes out when the owner says so would be gone — silently, with no code that looks
like it removed a gate.
"""

import pytest

from daemon import auto_dispatch_allowed


def test_gated_never_dispatches_by_itself():
    """The queue is a waiting room; Process empties it, per company."""
    assert not auto_dispatch_allowed("gated")


def test_auto_dispatches():
    assert auto_dispatch_allowed("auto")


def test_assisted_does_not():
    """Those are finished by hand in the owner's own browser."""
    assert not auto_dispatch_allowed("assisted")


@pytest.mark.parametrize("mode", ["", "GATED", "nonsense", None])
def test_anything_unrecognised_refuses(mode):
    """Fail closed. A typo in a flag must not start submitting applications."""
    assert not auto_dispatch_allowed(mode)


def test_recovery_is_not_gated_behind_dispatch():
    """Orphan recovery must run in EVERY mode.

    The loop used to reclaim orphans only on the idle path, after the dispatch
    checks. Adding the gated guard above it stranded every orphan in gated mode: a
    job whose worker was killed stayed leased, left the queue, and nothing put it
    back — which reads as a company quietly disappearing from the queue.

    This pins the ORDER by reading the source, because the behaviour lives in a
    `while True` that cannot be called directly.
    """
    import inspect

    import daemon

    src = inspect.getsource(daemon._apply_loop)
    reclaim = src.index("_reclaim_orphans(stores, q)")
    gate = src.index("if not auto_dispatch_allowed(")
    assert reclaim < gate, "recovery must run before the dispatch mode check"
