"""Gate-answer resume: fieldmap merge + answer-bank write + apply re-enqueue."""

from __future__ import annotations

import json

import pytest
from core.models import JobRecord, Status
from core.storage.answer_bank import AnswerBank
from core.storage.artifacts import ArtifactStore
from core.storage.tracking import TrackingStore
from whatsapp.commands import Deps, route
from whatsapp.processor import process, resume_gate

OWNER = "15550001111"
QUESTION = "What is your notice period?"


class FakeQueue:
    def __init__(self):
        self.messages = []

    def enqueue(self, url, body):
        self.messages.append((url, body))
        return "mid"


@pytest.fixture
def deps(applications_table, answer_bank_table, artifacts_bucket):
    return Deps(
        tracking=TrackingStore(applications_table),
        bank=AnswerBank(answer_bank_table),
        artifacts=ArtifactStore(artifacts_bucket),
        queue=FakeQueue(),
        apply_queue_url="https://sqs/apply",
        qa=lambda q: f"QA:{q}",
    )


def _seed_gated_job(deps, *, proposed_scope="global"):
    job = JobRecord(company="Acme", job_id="1", title="SWE", jd_url="u",
                    jd_text="x", location="R", ats="greenhouse")
    deps.tracking.put_new(job)
    # Persist a gate-time fieldmap the way the worker's gates.py will.
    deps.artifacts.put(
        "fieldmaps", f"{job.pk}.json",
        json.dumps({"Full name": "Sayantan Bhowmik"}).encode(), "application/json",
    )
    deps.tracking.set_status(
        job.pk,
        Status.NEEDS_HUMAN,
        gate_reason="unknown_field",
        gate_question=QUESTION,
        drafted_answer="30 days",
        proposed_scope=proposed_scope,
        gate_msg_id="wamid.GATE",
        company="Acme",
        fieldmap_key=f"fieldmaps/{job.pk}.json",
    )
    return job.pk


def _fieldmap(deps, pk):
    return json.loads(deps.artifacts.get(f"fieldmaps/{pk}.json"))


def test_resume_gate_merges_banks_and_reenqueues(deps):
    pk = _seed_gated_job(deps)

    resume_gate(pk, "45 days", "global", "Acme", deps=deps)

    fieldmap = _fieldmap(deps, pk)
    assert fieldmap[QUESTION] == "45 days"  # merged...
    assert fieldmap["Full name"] == "Sayantan Bhowmik"  # ...without clobbering
    assert deps.bank.lookup(QUESTION, "SomeOtherCo") == "45 days"  # global scope
    assert deps.queue.messages == [("https://sqs/apply", {"pk": pk})]  # fresh apply task
    row = deps.tracking.get(pk)
    assert row["status"] == Status.TAILORED.value
    assert row["gate_reason"] == ""


def test_resume_gate_company_scope_stays_company_only(deps):
    pk = _seed_gated_job(deps)

    resume_gate(pk, "45 days", "company", "Acme", deps=deps)

    assert deps.bank.lookup(QUESTION, "Acme") == "45 days"
    assert deps.bank.lookup(QUESTION, "SomeOtherCo") is None  # never leaks globally


def test_resume_gate_without_fieldmap_still_resumes(deps):
    job = JobRecord(company="Beta", job_id="2", title="SWE", jd_url="u",
                    jd_text="x", location="R", ats="lever")
    deps.tracking.put_new(job)
    deps.tracking.set_status(job.pk, Status.NEEDS_HUMAN, gate_reason="gated_mode",
                             company="Beta")

    resume_gate(job.pk, "", "global", "Beta", deps=deps)  # plain approval, no answer

    assert deps.queue.messages == [("https://sqs/apply", {"pk": job.pk})]
    assert deps.tracking.get(job.pk)["status"] == Status.TAILORED.value


def test_free_text_reply_to_open_gate_overrides_draft(deps):
    pk = _seed_gated_job(deps)
    msg = {"wa_id": OWNER, "text": "60 days", "context_id": "wamid.GATE"}

    reply = route(msg, deps=deps)

    assert _fieldmap(deps, pk)[QUESTION] == "60 days"  # the reply IS the answer
    assert deps.bank.lookup(QUESTION, "Anywhere") == "60 days"
    assert deps.queue.messages == [("https://sqs/apply", {"pk": pk})]
    assert "60 days" in reply


def test_ok_reply_approves_the_drafted_answer(deps):
    pk = _seed_gated_job(deps)
    msg = {"wa_id": OWNER, "text": "ok", "context_id": "wamid.GATE"}

    route(msg, deps=deps)

    assert _fieldmap(deps, pk)[QUESTION] == "30 days"
    assert deps.bank.lookup(QUESTION, "Anywhere") == "30 days"


def test_company_only_button_restricts_scope(deps):
    pk = _seed_gated_job(deps, proposed_scope="global")
    msg = {"wa_id": OWNER, "button_id": "company_only", "context_id": "wamid.GATE"}

    route(msg, deps=deps)

    assert deps.bank.lookup(QUESTION, "Acme") == "30 days"
    assert deps.bank.lookup(QUESTION, "SomeOtherCo") is None
    assert deps.queue.messages == [("https://sqs/apply", {"pk": pk})]


def test_approve_button_uses_proposed_scope(deps):
    _seed_gated_job(deps, proposed_scope="global")
    msg = {"wa_id": OWNER, "button_id": "approve", "context_id": "wamid.GATE"}

    route(msg, deps=deps)

    assert deps.bank.lookup(QUESTION, "SomeOtherCo") == "30 days"


def test_process_routes_and_sends_reply(deps):
    pk = _seed_gated_job(deps)

    sent = []

    class FakeClient:
        def send_text(self, wa_id, text):
            sent.append((wa_id, text))

    deps.client = FakeClient()
    update = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": OWNER,
                                    "type": "text",
                                    "text": {"body": "45 days"},
                                    "context": {"id": "wamid.GATE"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }

    reply = process(update, deps=deps)

    assert _fieldmap(deps, pk)[QUESTION] == "45 days"
    assert sent == [(OWNER, reply)]
