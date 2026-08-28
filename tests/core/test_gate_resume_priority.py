"""An application that stopped to ask a question resumes the moment it is answered.

The owner answers a gate and then watches nothing happen. Two separate reasons,
both real:

  * `next()` dispatches by `queued_at`, oldest first, and re-queuing stamps NOW.
    So answering a question sent that job to the BACK of 344 queued applications
    spanning 712 hours. It was the single most recently queued item every time.
  * In gated mode `auto_dispatch_allowed` is False, so the apply worker does not
    take anything off the queue at all. The answer went into a waiting room that
    only the Process button empties, per company — and Process would then have
    started that company's OLDEST job, not the one just answered.

The authorisation argument is what makes an exception safe here. A question gate
comes from an application ALREADY IN FLIGHT: the owner started it, the browser
filled the form, and it stopped on one field it had no approved answer for.
Answering that field continues work the owner already authorised. It is not a
new decision, so it does not need the decision gate.

A bare "Ready to apply?" approval is the opposite — that IS the decision, and in
gated mode the decision belongs to Process. So approvals stay ordinary.
"""

import fakeredis
import pytest

from core.apply_queue import ApplyQueue


@pytest.fixture
def q():
    return ApplyQueue(fakeredis.FakeRedis(decode_responses=True))


def test_an_answered_gate_goes_to_the_front(q):
    q.put("netflix#old", "Netflix", queued_at=1.0)
    q.put("waymo#older", "Waymo", queued_at=2.0)
    q.put("stripe#answered", "Stripe", priority=True)   # queued LAST, just now

    assert q.next()["pk"] == "stripe#answered"


def test_ordinary_work_is_still_first_come_first_served(q):
    q.put("netflix#1", "Netflix", queued_at=2.0)
    q.put("waymo#1", "Waymo", queued_at=1.0)

    assert q.next()["pk"] == "waymo#1"


def test_two_answered_gates_run_oldest_first(q):
    q.put("netflix#a", "Netflix", priority=True, queued_at=5.0)
    q.put("waymo#b", "Waymo", priority=True, queued_at=1.0)

    assert q.next()["pk"] == "waymo#b"


def test_the_company_lease_still_holds_for_an_answered_gate(q):
    """The rule the queue exists for does not bend for urgency. Two sessions on
    one employer share a login and cookies and can capture each other's page."""
    first = q.next() if q.put("replit#1", "Replit") is None else None
    first = q.next()
    q.put("replit#answered", "Replit", priority=True)

    assert q.next() is None, "Replit is already running one"
    q.done(first)
    assert q.next()["pk"] == "replit#answered"


def test_gated_mode_dispatches_only_answered_gates(q):
    """In gated mode the worker may take an answered gate and nothing else."""
    q.put("netflix#waiting", "Netflix", queued_at=1.0)
    q.put("stripe#answered", "Stripe", priority=True)

    item = q.next(priority_only=True)
    assert item["pk"] == "stripe#answered"

    q.done(item)
    assert q.next(priority_only=True) is None, "ordinary work still waits for Process"


def test_a_retry_keeps_its_place_at_the_front(q):
    """A resumed application that hits a flaky browser must not be demoted to the
    back of 344 jobs — that is the same disappearance, one step later.

    Read off the queue rather than dispatched, because a retry also carries a
    backoff and this is a claim about ORDER, not about skipping the wait.
    """
    import json

    q.put("stripe#answered", "Stripe", priority=True)
    item = q.next()
    q.done(item)

    q.retry(item, "browser held by another program")

    queued = [json.loads(r) for r in q.r.lrange("applyq:co:stripe", 0, -1)]
    assert [i["pk"] for i in queued] == ["stripe#answered"]
    assert queued[0]["priority"] is True, "the retry lost its priority"


def test_backoff_still_applies_to_an_answered_gate(q):
    """Priority is about ORDER, not about ignoring a wait that exists to stop a
    failing job from spinning."""
    import time

    q.put("stripe#answered", "Stripe", priority=True,
          not_before=time.time() + 3600)

    assert q.next() is None
    assert q.next(priority_only=True) is None
