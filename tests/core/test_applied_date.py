"""Application dates describe confirmed submissions, not later row edits."""

from datetime import datetime

import fakeredis
import pytest

from core.models import JobRecord, Status
from core.storage.local import RedisTracking
from server import _to_ui


@pytest.mark.parametrize("status", [Status.APPLIED, Status.APPLIED_MANUAL])
def test_confirmation_records_a_date_that_later_edits_do_not_move(status):
    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    job = JobRecord(company="Acme", job_id="1", title="Engineer", jd_url="u", jd_text="x")
    tracking.put_new(job)
    tracking.set_status(job.pk, Status.SUBMITTING)
    assert not tracking.get(job.pk).get("applied_at")
    tracking.set_status(job.pk, status, confirmation_id="received")
    applied_at = tracking.get(job.pk)["applied_at"]
    assert datetime.fromisoformat(applied_at).tzinfo is not None
    tracking.set_status(job.pk, status, profile_id="another-profile")
    assert tracking.get(job.pk)["applied_at"] == applied_at


def test_ui_exposes_application_date_separately_from_later_activity():
    row = {"pk": "acme#1", "status": "applied", "applied_at": "2026-08-01T12:00:00+00:00",
           "events": [{"at": "2026-09-07T12:00:00+00:00", "detail": "Profile updated"}]}
    result = _to_ui(row, None)  # no artifacts in this row
    assert result["applied_at"] == row["applied_at"]
    assert result["updated_at"] != result["applied_at"]


def test_missing_historical_date_is_not_invented_from_recent_activity():
    row = {"pk": "acme#1", "status": "applied",
           "events": [{"at": "2026-09-07T12:00:00+00:00", "detail": "Profile updated"}]}
    assert _to_ui(row, None)["applied_at"] == ""
