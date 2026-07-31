"""Counting jobs must not mean reading every job.

The dashboard polls /stats every three seconds. Counting by walking the rows
took 34 seconds at 1737 rows, so the board spent its entire life waiting on a
count and every click felt dead. The status sets are maintained on write, so the
same answer is a handful of set cardinalities.
"""

import fakeredis
import pytest

from core.models import JobRecord, Status
from core.storage.local import RedisTracking


@pytest.fixture
def t():
    return RedisTracking(fakeredis.FakeRedis(decode_responses=True))


def job(pk_company, jid):
    return JobRecord(company=pk_company, job_id=jid, title="Engineer",
                     jd_url=f"https://x/{jid}", jd_text="x")


def test_counts_match_the_rows_they_describe(t):
    for i in range(7):
        t.put_new(job("Acme", f"j{i}"))
    t.set_status("acme#j0", Status.TAILORED)
    t.set_status("acme#j1", Status.APPLIED)

    counts = t.status_counts()
    assert counts.get("found") == 5
    assert counts.get("tailored") == 1
    assert counts.get("applied") == 1
    assert sum(counts.values()) == 7, "every job counted exactly once"


def test_a_status_change_moves_the_job_rather_than_duplicating_it(t):
    t.put_new(job("Acme", "j0"))
    t.set_status("acme#j0", Status.TAILORED)
    t.set_status("acme#j0", Status.APPLIED)
    counts = t.status_counts()
    assert counts.get("found", 0) == 0 and counts.get("tailored", 0) == 0
    assert counts.get("applied") == 1
    assert sum(counts.values()) == 1


def test_bookkeeping_rows_are_not_counted_as_jobs(t):
    """Watermarks and run markers carry a status too. Letting them into the index
    made every count wrong by however many existed: `found` read 1230 when 1200
    jobs were found."""
    t.put_new(job("Acme", "j0"))
    t.set_status("meta#run#acme", Status.FOUND)
    t.set_status("meta#watermark#acme", Status.FOUND)

    assert t.status_counts().get("found") == 1, "only the real job counts"


def test_an_empty_store_counts_nothing_rather_than_failing(t):
    assert t.status_counts() == {}
