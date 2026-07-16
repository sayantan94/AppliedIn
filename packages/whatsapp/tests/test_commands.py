"""Command router: slash commands, kill switch, button taps, Q&A fallback."""

from __future__ import annotations

import pytest
from appliedin_core.models import JobRecord, Status
from appliedin_core.storage.answer_bank import AnswerBank
from appliedin_core.storage.artifacts import ArtifactStore
from appliedin_core.storage.tracking import TrackingStore
from appliedin_whatsapp.commands import Deps, is_paused, route

OWNER = "15550001111"


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


def _seed_job(deps, job_id="1"):
    job = JobRecord(
        company="Acme", job_id=job_id, title="SWE", jd_url="u",
        jd_text="python things", location="Remote", ats="greenhouse",
    )
    assert deps.tracking.put_new(job)
    return job.pk


def _text(text):
    return {"wa_id": OWNER, "text": text}


def test_done_marks_applied_manual(deps):
    pk = _seed_job(deps)
    reply = route(_text(f"/done {pk}"), deps=deps)
    assert deps.tracking.get(pk)["status"] == Status.APPLIED_MANUAL.value
    assert pk in reply


def test_skip_marks_skipped(deps):
    pk = _seed_job(deps)
    reply = route(_text(f"/skip {pk}"), deps=deps)
    assert deps.tracking.get(pk)["status"] == Status.SKIPPED.value
    assert pk in reply


def test_skip_unknown_id_is_reported_not_written(deps):
    reply = route(_text("/skip nope#42"), deps=deps)
    assert "Unknown job" in reply
    assert deps.tracking.get("nope#42") is None


def test_fact_lands_in_global_bank(deps):
    route(_text("/fact Do you require sponsorship? = Yes, H-1B"), deps=deps)
    # Global scope: visible from any company, with the normalized-label lookup.
    assert deps.bank.lookup("do you require  sponsorship", "SomeOtherCo") == "Yes, H-1B"


def test_fact_without_equals_is_usage_error(deps):
    reply = route(_text("/fact just some text"), deps=deps)
    assert "Usage" in reply
    assert deps.bank.lookup("just some text", "Acme") is None


def test_pause_and_resume_toggle_killswitch(deps):
    assert is_paused(deps.tracking) is False
    route(_text("/pause"), deps=deps)
    assert is_paused(deps.tracking) is True
    route(_text("/resume"), deps=deps)
    assert is_paused(deps.tracking) is False


def test_status_counts_exclude_meta_rows(deps):
    pk = _seed_job(deps)
    deps.tracking.set_status(pk, Status.APPLIED)
    route(_text("/pause"), deps=deps)  # writes the meta#killswitch flag row

    reply = route(_text("/status"), deps=deps)
    assert "applied: 1" in reply
    assert "PAUSED" in reply
    assert "skipped" not in reply  # the killswitch flag row must not leak into counts


def test_free_text_goes_to_qa_agent(deps):
    reply = route(_text("why was the Acme one skipped?"), deps=deps)
    assert reply == "QA:why was the Acme one skipped?"


def test_free_text_with_unrelated_context_still_goes_to_qa(deps):
    pk = _seed_job(deps)
    deps.tracking.set_status(
        pk, Status.NEEDS_HUMAN, gate_reason="unknown_field", gate_msg_id="wamid.GATE"
    )
    msg = {"wa_id": OWNER, "text": "what did we send?", "context_id": "wamid.OTHER"}
    assert route(msg, deps=deps) == "QA:what did we send?"


def test_skip_button_tap_resolves_single_open_gate(deps):
    pk = _seed_job(deps)
    deps.tracking.set_status(pk, Status.NEEDS_HUMAN, gate_reason="captcha")

    reply = route({"wa_id": OWNER, "button_id": "skip"}, deps=deps)
    assert deps.tracking.get(pk)["status"] == Status.SKIPPED.value
    assert pk in reply


def test_manual_button_keeps_needs_human(deps):
    pk = _seed_job(deps)
    deps.tracking.set_status(pk, Status.NEEDS_HUMAN, gate_reason="captcha")

    reply = route({"wa_id": OWNER, "button_id": "i_ll_do_it_manually"}, deps=deps)
    row = deps.tracking.get(pk)
    assert row["status"] == Status.NEEDS_HUMAN.value
    assert row["manual"] is True
    assert f"/done {pk}" in reply


def test_retry_button_reenqueues_apply(deps):
    pk = _seed_job(deps)
    deps.tracking.set_status(pk, Status.NEEDS_HUMAN, gate_reason="no_account")

    route({"wa_id": OWNER, "button_id": "account_created_retry"}, deps=deps)
    assert deps.queue.messages == [("https://sqs/apply", {"pk": pk})]


def test_button_tap_with_context_targets_the_right_gate(deps):
    pk1, pk2 = _seed_job(deps, "1"), _seed_job(deps, "2")
    deps.tracking.set_status(pk1, Status.NEEDS_HUMAN, gate_reason="captcha", gate_msg_id="wamid.A")
    deps.tracking.set_status(pk2, Status.NEEDS_HUMAN, gate_reason="captcha", gate_msg_id="wamid.B")

    route({"wa_id": OWNER, "button_id": "skip", "context_id": "wamid.B"}, deps=deps)
    assert deps.tracking.get(pk2)["status"] == Status.SKIPPED.value
    assert deps.tracking.get(pk1)["status"] == Status.NEEDS_HUMAN.value


def test_button_tap_with_ambiguous_gates_asks_for_clarity(deps):
    pk1, pk2 = _seed_job(deps, "1"), _seed_job(deps, "2")
    deps.tracking.set_status(pk1, Status.NEEDS_HUMAN, gate_reason="captcha")
    deps.tracking.set_status(pk2, Status.NEEDS_HUMAN, gate_reason="captcha")

    reply = route({"wa_id": OWNER, "button_id": "skip"}, deps=deps)
    assert "couldn't match" in reply
    assert deps.tracking.get(pk1)["status"] == Status.NEEDS_HUMAN.value
    assert deps.tracking.get(pk2)["status"] == Status.NEEDS_HUMAN.value


def test_raw_meta_update_is_parsed(deps):
    pk = _seed_job(deps)
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
                                    "text": {"body": f"/done {pk}"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    route(update, deps=deps)
    assert deps.tracking.get(pk)["status"] == Status.APPLIED_MANUAL.value


def test_unknown_command_returns_help(deps):
    assert "Commands:" in route(_text("/frobnicate"), deps=deps)
