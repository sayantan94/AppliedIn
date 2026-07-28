"""Applications are grouped per company, retried, and eventually given up on.

The rules encoded here are the ones with consequences. Retrying the wrong failure
sends a second application under a real name; draining one company at a time is
the pattern an employer's automation defences look for.
"""

import fakeredis
import pytest

from core import apply_queue as aq
from core.apply_queue import ApplyQueue, is_retryable


@pytest.fixture
def q():
    return ApplyQueue(fakeredis.FakeRedis(decode_responses=True))


def test_never_two_applications_to_the_same_company_at_once(q):
    """The rule the whole design exists for.

    Two sessions on one employer share a domain, a login and cookies; they
    interleave and can capture each other's page. Different employers do not
    collide, so they may run together.
    """
    q.put("waymo#1", "Waymo")
    q.put("waymo#2", "Waymo")
    q.put("netflix#1", "Netflix")

    first = q.next()
    assert first["company"] == "Waymo", "first come first served"

    second = q.next()
    assert second is not None, "a DIFFERENT company may start straight away"
    assert second["company"] == "Netflix"

    assert q.next() is None, "waymo#2 must wait: Waymo already has one running"

    q.done(first)                       # the first Waymo application finishes
    third = q.next()
    assert third is not None and third["pk"] == "waymo#2", "now it may start"


def test_a_lease_is_released_even_when_the_application_fails(q):
    """Released in a finally by the worker. A lease left behind by a crash would
    block that employer until the process restarted."""
    q.put("acme#1", "Acme")
    q.put("acme#2", "Acme")
    item = q.next()
    assert q.next() is None
    q.done(item)                        # what the worker's finally does
    assert q.next() is not None


def test_startup_clears_stale_leases(q):
    """Leases describe what is running NOW, and nothing runs in a process that has
    only just started."""
    q.put("acme#1", "Acme")
    q.put("acme#2", "Acme")
    q.next()                            # leaves Acme leased
    assert q.next() is None
    assert q.reset_leases() == 1
    assert q.next() is not None


def test_dispatch_is_first_come_first_served(q):
    """No pacing and no rotation: whatever was queued longest ago goes next."""
    import time as _t
    q.put("a#1", "Alpha", queued_at=_t.time() - 30)
    q.put("b#1", "Beta", queued_at=_t.time() - 10)
    q.put("c#1", "Gamma", queued_at=_t.time() - 20)
    assert [q.next()["company"] for _ in range(3)] == ["Alpha", "Gamma", "Beta"]


def test_an_unconfirmed_submit_is_never_retried():
    """The one that would double apply.

    "uncertain" means the form may already have gone through. Retrying it could
    submit a second application under a real name, which is the single thing this
    system must never do, so it goes to the owner instead of back to the queue.
    """
    again, _ = is_retryable({"status": "uncertain", "detail": "browser closed"})
    assert not again


@pytest.mark.parametrize("result", [
    {"status": "applied", "confirmation": "thanks"},
    {"status": "failed", "reason": "duplicate_application"},
    {"status": "failed", "reason": "guardrail"},
    {"status": "failed", "reason": "application_limit"},
    {"status": "gate", "reason": "unknown_field"},
])
def test_terminal_outcomes_are_not_retried(result):
    again, _ = is_retryable(result)
    assert not again, f"{result} must not be retried"


@pytest.mark.parametrize("result", [
    {"status": "failed", "reason": "browser_conflict"},
    {"status": "unknown", "detail": "could not read the posting"},
    {"status": "error", "reason": "timeout"},
])
def test_environment_failures_are_retried(result):
    again, _ = is_retryable(result)
    assert again, f"{result} is transient and should be retried"


def test_it_gives_up_and_dead_letters_with_its_history(q, monkeypatch):
    """A job that keeps failing has to become countable rather than a card that
    quietly stopped moving."""
    monkeypatch.setattr(aq, "BACKOFF_S", (0, 0, 0))
    q.put("acme#1", "Acme")

    item = q.next()
    for _ in range(aq.MAX_ATTEMPTS - 1):
        assert q.retry(item, "browser_conflict") is True
        q.done(item)
        item = q.next()
        assert item is not None
    assert q.retry(item, "browser_conflict") is False, "should have given up by now"

    dlq = q.dead_letters()
    assert len(dlq) == 1
    assert dlq[0]["pk"] == "acme#1"
    assert len(dlq[0]["history"]) == aq.MAX_ATTEMPTS, "each attempt should be recorded"


def test_reviving_a_dead_letter_resets_its_attempts(q, monkeypatch):
    """Asking again is a new decision, not a continuation of the failed run."""
    monkeypatch.setattr(aq, "BACKOFF_S", (0, 0, 0))
    q.put("acme#1", "Acme")
    item = q.next()
    while q.retry(item, "browser_conflict"):
        q.done(item)
        item = q.next()
    q.done(item)

    assert q.revive() == 1
    assert q.dead_letters() == []
    back = q.next()
    assert back["pk"] == "acme#1"
    assert back["attempts"] == 0, "a revived job starts its attempts over"


def test_backoff_holds_a_job_until_its_time(q, monkeypatch):
    monkeypatch.setattr(aq, "BACKOFF_S", (10_000,))
    q.put("acme#1", "Acme")
    item = q.next()
    q.retry(item, "browser_conflict")
    q.done(item)
    assert q.next() is None, "a job in backoff must not be handed out early"
