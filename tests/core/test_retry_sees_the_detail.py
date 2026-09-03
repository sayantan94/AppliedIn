"""An outage must not spend a job's attempts — and retry() can only tell an
outage from a fault by the words it is handed.

`is_retryable` returns the CODE ("unknown"); `retry()` classifies by the DETAIL
("Rate limited by the model API partway through…"). Handing it the code meant
every rate-limited apply read as a genuine failure: netflix#790315245272 burned
attempt 2 of 3 on a rate limit, one more and it would have dead-lettered a job
no browser had ever opened a form for."""

from __future__ import annotations

import inspect

import fakeredis

from core.apply_queue import ApplyQueue


def test_run_queued_hands_retry_the_detail():
    from agent import run

    src = inspect.getsource(run.run_queued)
    assert 'q.retry(item, str(result.get("detail") or why))' in src


def test_a_rate_limit_costs_no_attempt():
    q = ApplyQueue(fakeredis.FakeRedis(decode_responses=True))
    item = {"pk": "netflix#1", "company": "Netflix", "attempts": 0, "history": []}
    detail = ("Rate limited by the model API partway through, after 1 steps. "
              "Nothing was submitted. This retries by itself.")

    assert q.retry(item, detail) is True
    import json
    queued = [json.loads(x) for x in q.r.lrange("applyq:co:netflix", 0, -1)]
    assert queued[0]["attempts"] == 0, "an outage spent an attempt"


def test_a_session_killed_by_a_restart_costs_no_attempt():
    from tools.claude_chrome import is_infrastructure

    detail = ("The browser agent finished without confirming a submission: "
              "claude --chrome failed (exit 143): it produced no output at all")
    assert is_infrastructure(detail)
    assert not is_infrastructure("The browser agent finished without confirming a submission: "
                                 "claude --chrome failed (exit 1): bad flag")
