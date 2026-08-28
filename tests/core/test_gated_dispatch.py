"""In gated mode the worker takes an answered question, and nothing else.

`auto_dispatch_allowed` is False in gated mode, and the apply loop then slept
without looking at the queue at all. That is right for ordinary work: the tailor
puts every finished job in the queue, so draining it automatically would submit
applications nobody approved and the queue would quietly become an auto-apply
pipeline. Process, per company, is the owner's decision point and stays there.

An application that stopped to ask a question is not waiting for that decision.
It was already started, already filled, and it stopped on one field. Answering
that field continues work the owner authorised when they started it, so the
worker may pick it up — and only it.
"""

from __future__ import annotations

import fakeredis
import pytest

from core.apply_queue import ApplyQueue
from daemon import next_dispatchable


@pytest.fixture
def q():
    q = ApplyQueue(fakeredis.FakeRedis(decode_responses=True))
    q.put("netflix#waiting", "Netflix", queued_at=1.0)
    q.put("stripe#answered", "Stripe", priority=True, queued_at=9.0)
    return q


def test_gated_takes_the_answered_question_only(q):
    item = next_dispatchable(q, "gated")
    assert item["pk"] == "stripe#answered"

    q.done(item)
    assert next_dispatchable(q, "gated") is None, \
        "ordinary work must still wait for Process"


def test_assisted_behaves_the_same(q):
    """Assisted applications are finished by hand, but a question the owner
    answered is still theirs to have acted on."""
    assert next_dispatchable(q, "assisted")["pk"] == "stripe#answered"


def test_auto_takes_everything_answered_questions_first(q):
    assert next_dispatchable(q, "auto")["pk"] == "stripe#answered"
    assert next_dispatchable(q, "auto")["pk"] == "netflix#waiting"


def test_an_empty_queue_is_idle_not_an_error():
    empty = ApplyQueue(fakeredis.FakeRedis(decode_responses=True))
    assert next_dispatchable(empty, "gated") is None
    assert next_dispatchable(empty, "auto") is None
