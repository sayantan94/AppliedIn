"""Answering the question is what starts the application again.

The owner answers a gate and watches nothing happen. `resume_job` did re-queue
the job — but as ordinary work, stamped with the current time, which put it
behind 344 applications spanning 712 hours, in a gated board where the worker
takes nothing off the queue at all.

The distinction that makes resuming safe is WHICH gate was answered:

  a question   the application was already running and stopped on one field it
               had no approved answer for. The decision to apply was made when
               that job was started; the answer continues it.
  an approval  "Ready to apply?" IS the decision. In gated mode that decision
               belongs to Process, and it stays there.
"""

from __future__ import annotations

import fakeredis
import pytest

from agent.run import resume_job
from core.apply_queue import ApplyQueue


class _Tracking:
    def __init__(self, row):
        self.row, self.r = dict(row), fakeredis.FakeRedis(decode_responses=True)

    def get(self, pk):
        return dict(self.row)

    def set_status(self, pk, status, **kw):
        self.row["status"] = getattr(status, "value", status)
        self.row.update(kw)


class _Bank:
    def __init__(self):
        self.puts = []

    def put(self, question, answer, scope, **kw):
        self.puts.append((question, answer, scope))


class _Stores:
    def __init__(self, row):
        self.tracking, self.answer_bank = _Tracking(row), _Bank()


def _queued(stores):
    import json

    r = stores.tracking.r
    out = []
    for co in r.smembers("applyq:companies"):
        out += [json.loads(x) for x in r.lrange(f"applyq:co:{co}", 0, -1)]
    return out


@pytest.fixture
def question_row():
    return {"pk": "replit#1", "company": "Replit", "status": "needs_human",
            "gate_call_id": "direct", "gate_source": "applier",
            "gate_reason": "unknown_field",
            "gate_pending": {"question": "The form has a required field "
                                         "'What is your desired salary range?' "
                                         "and no approved answer."}}


def test_an_answered_question_is_queued_to_run_next(question_row):
    stores = _Stores(question_row)

    resume_job("replit#1", "$300,000 base", stores=stores)

    items = _queued(stores)
    assert [i["pk"] for i in items] == ["replit#1"]
    assert items[0]["priority"] is True, "the answer did not start the application"


def test_it_is_taken_ahead_of_everything_already_waiting(question_row):
    stores = _Stores(question_row)
    q = ApplyQueue(stores.tracking.r)
    q.put("netflix#1", "Netflix", queued_at=1.0)
    q.put("waymo#1", "Waymo", queued_at=2.0)

    resume_job("replit#1", "$300,000 base", stores=stores)

    assert q.next()["pk"] == "replit#1"


def test_a_bare_approval_stays_ordinary_work():
    """The approval IS the decision. Letting it jump the gated queue would turn
    Process into decoration and the queue into an auto-apply pipeline."""
    stores = _Stores({"pk": "replit#2", "company": "Replit", "status": "tailored",
                      "gate_call_id": "call_x", "gate_source": "applier",
                      "gate_reason": "approval",
                      "gate_pending": {"question": "Ready to apply to Replit? The "
                                                   "tailored résumé is saved."}})

    resume_job("replit#2", "approved", stores=stores)

    items = _queued(stores)
    assert [i["pk"] for i in items] == ["replit#2"]
    assert items[0]["priority"] is False
