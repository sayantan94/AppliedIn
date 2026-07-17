"""Handler routing: skip / truthfulness gate / assist gate / happy path.

score_match, tailor, and render_pdf are stubbed at the handler-module level;
the truthfulness validator runs for real (it is deterministic and cheap).
"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from core.models import ApplyMode, JobRecord, Status
from core.storage.artifacts import ArtifactStore
from core.storage.tracking import TrackingStore
from tailoring import handler as h

BASE = {
    "experience": [
        {
            "employer": "Acme",
            "title": "Senior Engineer",
            "start": "2020-01",
            "end": "2023-06",
            "bullets": ["Built the billing service"],
        }
    ],
    "education": [{"degree": "BSc Computer Science", "institution": "IIT Kharagpur"}],
    "certifications": [],
}


class FakeQueue:
    def __init__(self):
        self.messages = []

    def enqueue(self, url, body):
        self.messages.append((url, body))
        return "mid"


@pytest.fixture
def env(applications_table, artifacts_bucket):
    tracking = TrackingStore(applications_table)
    artifacts = ArtifactStore(artifacts_bucket)
    job = JobRecord(
        company="Acme", job_id="1", title="SWE", jd_url="u",
        jd_text="python backend", location="Remote", ats="greenhouse",
    )
    assert tracking.put_new(job)
    return tracking, artifacts, FakeQueue(), job.pk


def _process(env, modes=None):
    tracking, artifacts, queue, pk = env
    result = h.process_record(
        pk,
        tracking=tracking,
        artifacts=artifacts,
        queue=queue,
        base=BASE,
        min_match_score=7,
        modes=modes or {},
        apply_queue_url="apply-url",
    )
    return result, tracking.get(pk), queue


def test_low_score_skips_and_never_enqueues(env, monkeypatch):
    monkeypatch.setattr(h, "score_match", lambda jd, base, model=None: 3)
    monkeypatch.setattr(h, "tailor", lambda *a, **k: pytest.fail("tailor must not run"))

    result, row, queue = _process(env)

    assert result == Status.SKIPPED.value
    assert row["status"] == Status.SKIPPED.value
    assert "3 < 7" in row["skip_reason"]
    assert queue.messages == []


def test_truthfulness_violation_gates_without_enqueue(env, monkeypatch):
    invented = deepcopy(BASE)
    invented["experience"][0]["employer"] = "Initech"  # invented employer
    monkeypatch.setattr(h, "score_match", lambda jd, base, model=None: 9)
    monkeypatch.setattr(h, "tailor", lambda base, jd, model=None: invented)
    monkeypatch.setattr(
        h, "render_pdf", lambda t: pytest.fail("render must not run on violation")
    )

    result, row, queue = _process(env)

    assert result == Status.NEEDS_HUMAN.value
    assert row["status"] == Status.NEEDS_HUMAN.value
    assert any("Initech" in v for v in row["truthfulness_diff"])
    assert queue.messages == []


def test_happy_path_tailors_stores_pdf_and_enqueues(env, monkeypatch):
    monkeypatch.setattr(h, "score_match", lambda jd, base, model=None: 9)
    monkeypatch.setattr(h, "tailor", lambda base, jd, model=None: deepcopy(BASE))
    monkeypatch.setattr(h, "render_pdf", lambda t: b"%PDF-fake")

    result, row, queue = _process(env)
    tracking, artifacts, _, pk = env

    assert result == Status.TAILORED.value
    assert row["status"] == Status.TAILORED.value
    assert row["resume_s3_key"] == f"resumes/{pk}-1.pdf"
    assert int(row["resume_version"]) == 1
    assert artifacts.get(row["resume_s3_key"]) == b"%PDF-fake"
    assert queue.messages == [("apply-url", {"pk": pk})]


def test_assist_mode_gates_to_human_with_resume_ready(env, monkeypatch):
    monkeypatch.setattr(h, "score_match", lambda jd, base, model=None: 9)
    monkeypatch.setattr(h, "tailor", lambda base, jd, model=None: deepcopy(BASE))
    monkeypatch.setattr(h, "render_pdf", lambda t: b"%PDF-fake")

    result, row, queue = _process(env, modes={"acme": ApplyMode.ASSIST})

    assert result == Status.NEEDS_HUMAN.value
    assert row["status"] == Status.NEEDS_HUMAN.value
    assert row["resume_s3_key"]  # resume is prepared for the human
    assert queue.messages == []  # never auto-applies in assist mode


def test_handler_processes_sqs_batch(env, monkeypatch):
    tracking, artifacts, queue, pk = env
    monkeypatch.setattr(h, "score_match", lambda jd, base, model=None: 9)
    monkeypatch.setattr(h, "tailor", lambda base, jd, model=None: deepcopy(BASE))
    monkeypatch.setattr(h, "render_pdf", lambda t: b"%PDF-fake")
    monkeypatch.setattr(h, "TrackingStore", lambda *a, **k: tracking)
    monkeypatch.setattr(h, "ArtifactStore", lambda *a, **k: artifacts)
    monkeypatch.setattr(h, "Queue", lambda *a, **k: queue)
    monkeypatch.setattr(h, "load_base_resume", lambda p: BASE)
    monkeypatch.setattr(h, "load_min_match_score", lambda p: 7)
    monkeypatch.setattr(h, "load_company_modes", lambda p: {})

    event = {"Records": [{"body": json.dumps({"pk": pk})}]}
    results = h.handler(event, None)

    assert results == {pk: Status.TAILORED.value}
    assert queue.messages and queue.messages[0][1] == {"pk": pk}
