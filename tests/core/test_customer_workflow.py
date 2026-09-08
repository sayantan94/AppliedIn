"""Review and tracker actions must not submit or rewrite application history."""
from types import SimpleNamespace
from unittest.mock import Mock

import fakeredis
import pytest

from core.application_tracker import validate
from core.models import JobRecord, Status
from core.storage.local import RedisTracking
from server import _to_ui


def test_notes_survive_status_changes_without_moving_the_application_date():
    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    job = JobRecord(company="Acme", job_id="1", title="Engineer", jd_url="u", jd_text="x")
    tracking.put_new(job)
    tracking.set_status(job.pk, Status.APPLIED)
    before = tracking.get(job.pk)
    note = validate({"notes": "Contact recruiter", "outcome": "interview",
                     "follow_up": "2026-09-10"})
    tracking.save_application_note(job.pk, note)
    assert tracking.get(job.pk) == before, "a tracker edit must not rewrite pipeline status"
    tracking.set_status(job.pk, Status.APPLIED, confirmation_id="received")
    assert tracking.application_notes()[job.pk] == note
    assert tracking.get(job.pk)["applied_at"] == before["applied_at"]


@pytest.mark.parametrize("body", [
    {"follow_up": "2026-02-30"}, {"follow_up": "20260901"},
    {"outcome": "submitted"}, {"outcome": []}, {"notes": "x" * 10001},
])
def test_invalid_tracker_input_cannot_be_saved(body):
    with pytest.raises(ValueError):
        validate(body)


def test_old_preparation_dates_can_be_recovered_from_a_completion_event():
    row = {"pk": "acme#1", "events": [
        {"status": "tailored", "at": "2026-08-01T12:00:00Z"},
        {"status": "submitting", "at": "2026-09-07T12:00:00Z"}]}
    assert _to_ui(row, None)["tailored_at"] == "2026-08-01T12:00:00Z"
    assert _to_ui(row, None)["applied_at"] == ""


def test_review_graph_has_no_applier_or_submission_tools():
    from agent.graph import review_agent

    def agents(node):
        yield node
        for child in node.sub_agents:
            yield from agents(child)

    names = {a.name for a in agents(review_agent)}
    assert "scorer" in names and "tailor" in names and "critic" in names
    assert "applier" not in names
    for agent in agents(review_agent):
        for tool in getattr(agent, "tools", []):
            assert getattr(tool, "__name__", "") != "apply_to_job"


@pytest.mark.asyncio
@pytest.mark.parametrize("pdf, expected", [
    ("resumes/1.pdf", "prepared"), ("resumes/1.tex", "failed"),
])
async def test_review_finishes_without_dispatching_any_application(
        pdf, expected, monkeypatch):
    from agent.run import _drive_async
    from core import events
    monkeypatch.setattr(events, "emit", lambda *a, **kw: None)
    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    tracking.set_status("acme#1", Status.TAILORING, resume_s3_key=pdf)
    stores = SimpleNamespace(tracking=tracking, queue=Mock())

    class Runner:
        async def run_async(self, **kw):
            if False:
                yield None

    result = await _drive_async(Runner(), "acme#1", None, stores, prepare_only=True)
    assert result["result"] == expected
    stores.queue.enqueue.assert_not_called()
    assert not tracking.get("acme#1").get("applied_at")
    if expected == "prepared":
        assert tracking.get("acme#1")["tailored_at"]
        assert tracking.get("acme#1")["status"] == "tailored"
