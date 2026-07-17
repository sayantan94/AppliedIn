from core.models import JobRecord, Status
from core.storage.tracking import TrackingStore

from .conftest import make_applications_table


def _job(job_id="1", jd_text="x"):
    return JobRecord(
        company="Acme", job_id=job_id, title="SWE", jd_url="u",
        jd_text=jd_text, location="R", ats="greenhouse",
    )


def test_put_new_dedups(aws):
    make_applications_table()
    store = TrackingStore("applications")
    job = _job()
    assert store.put_new(job) is True
    assert store.put_new(job) is False


def test_find_by_jd_hash(aws):
    make_applications_table()
    store = TrackingStore("applications")
    job = _job(job_id="1", jd_text="build the thing")
    store.put_new(job)
    assert store.find_by_jd_hash(job.jd_hash) == "acme#1"
    assert store.find_by_jd_hash("deadbeef") is None


def test_set_and_query_status(aws):
    make_applications_table()
    store = TrackingStore("applications")
    store.put_new(_job("1"))
    store.set_status("acme#1", Status.CAPPED)
    rows = store.query_status(Status.CAPPED)
    assert [r["pk"] for r in rows] == ["acme#1"]


def test_daily_cap_atomic(aws):
    make_applications_table()
    store = TrackingStore("applications")
    assert all(store.try_increment_daily_cap("2026-07-16", cap=2) for _ in range(2))
    assert store.try_increment_daily_cap("2026-07-16", cap=2) is False
    # a different day has its own counter
    assert store.try_increment_daily_cap("2026-07-17", cap=2) is True
