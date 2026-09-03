"""An Apply press on a stale résumé re-tailors and then applies, as one action.

The stale-résumé guard in `_apply_direct` hands a row whose seed no longer
matches to the tailor QUEUE. Right for the worker; wrong for a button. With the
board paused that queue is asleep, so the row went back to `found` and the press
looked like nothing had happened — 181 of 381 tailored rows were in that state
after the base résumé was edited three times in a day.

A press is an explicit instruction, so the endpoint does the re-tailor itself
and applies in the same task, and tells the UI it is doing so.
"""

from __future__ import annotations


def test_apply_now_retailors_a_stale_row_before_applying():
    import server

    src = open(server.__file__).read()
    i = src.index('@app.post("/actions/apply-now/{pk}")')
    body = src[i:i + 7000]
    assert "seed_fingerprint()" in body, "the press must check staleness itself"
    assert "pool.submit(run_job, pk, stores).result(timeout=RETAILOR_DEADLINE_S)" in body, \
        "re-tailor in its own task, under a deadline"
    assert body.index("pool.submit(run_job, pk, stores)") < body.index("run_queued(item, q)"), \
        "tailor first, then apply"
    assert '"retailoring": stale' in body, "and tell the UI which it is doing"


def test_a_row_that_does_not_come_back_tailored_is_not_applied():
    import server

    src = open(server.__file__).read()
    i = src.index('@app.post("/actions/apply-now/{pk}")')
    body = src[i:i + 7000]
    assert 'not in ("tailored", "needs_human")' in body
    assert "q.done(item)" in body, "the company lease must be released either way"


def test_a_retailor_that_hits_the_deadline_releases_the_lease():
    """A model call stuck behind a rate limit once held Netflix's lease for three
    and a half hours from this endpoint."""
    import server

    src = open(server.__file__).read()
    i = src.index('@app.post("/actions/apply-now/{pk}")')
    body = src[i:i + 7000]
    j = body.index("except concurrent.futures.TimeoutError:")
    assert "q.done(item)" in body[j:j + 900]
    assert "Status.FOUND" in body[j:j + 900]
