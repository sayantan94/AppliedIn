"""Control-flow tests for the agentic pipeline — the custom-logic core.

Step functions are faked so these test ONLY the orchestration: the sequential
advance, gate-persist-and-stop, resume-from-the-gated-step, and terminal states.
"""

from __future__ import annotations

from core.models import AnswerScope, GateReason, Status
from core.storage.answer_bank import AnswerBank
from core.storage.tracking import TrackingStore
from pipeline.orchestrator import PipelineDeps, resume, run
from pipeline.steps import Step, advance, gate, terminal


def _seed(tables, **over):
    apps, _ = tables
    store = TrackingStore(apps)
    from core.models import JobRecord
    store.put_new(JobRecord(company="Acme", job_id="1", title="SWE", jd_url="u",
                            jd_text="python", location="Remote", ats="greenhouse"))
    if over:
        store.set_status("acme#1", Status.FOUND, **over)
    return store


def _deps(tables, steps):
    apps, bank = tables
    return PipelineDeps(
        tracking=TrackingStore(apps),
        answer_bank=AnswerBank(bank),
        steps=steps,
        now=lambda: "2026-07-16T00:00:00Z",
    )


def test_runs_all_steps_to_applied(tables):
    store = _seed(tables)
    steps = {
        Step.ENSURE_ACCOUNT: lambda pk, row, d: advance(Status.FOUND),
        Step.PROCESS_JD: lambda pk, row, d: advance(Status.FOUND, match_score=9),
        Step.TWEAK_RESUME: lambda pk, row, d: advance(Status.TAILORED, resume_version="v4"),
        Step.FILL_INFO: lambda pk, row, d: advance(Status.TAILORED),
        Step.SUBMIT: lambda pk, row, d: terminal(Status.APPLIED, confirmation_id="OK-1"),
    }
    result = run("acme#1", _deps(tables, steps))
    assert result == {"result": "terminal", "status": "applied"}
    row = store.get("acme#1")
    assert row["status"] == "applied"
    assert row["confirmation_id"] == "OK-1"
    assert row["match_score"] == 9
    assert len(row["events"]) == 5  # one per step


def test_low_score_terminates_at_process_jd(tables):
    store = _seed(tables)
    steps = {
        Step.ENSURE_ACCOUNT: lambda pk, row, d: advance(Status.FOUND),
        Step.PROCESS_JD: lambda pk, row, d: terminal(Status.SKIPPED, match_score=4,
                                                     skip_reason="low"),
        Step.TWEAK_RESUME: lambda pk, row, d: advance(Status.TAILORED),
        Step.FILL_INFO: lambda pk, row, d: advance(Status.TAILORED),
        Step.SUBMIT: lambda pk, row, d: terminal(Status.APPLIED),
    }
    result = run("acme#1", _deps(tables, steps))
    assert result["status"] == "skipped"
    assert store.get("acme#1")["pipeline_step"] == "process_jd"


def test_gate_persists_step_and_stops(tables):
    store = _seed(tables)
    steps = {
        Step.ENSURE_ACCOUNT: lambda pk, row, d: advance(Status.FOUND),
        Step.PROCESS_JD: lambda pk, row, d: advance(Status.FOUND, match_score=8),
        Step.TWEAK_RESUME: lambda pk, row, d: advance(Status.TAILORED),
        Step.FILL_INFO: lambda pk, row, d: gate(GateReason.LOW_CONFIDENCE,
                                                "Why us?", question="Why us?"),
        Step.SUBMIT: lambda pk, row, d: terminal(Status.APPLIED),
    }
    result = run("acme#1", _deps(tables, steps))
    assert result == {"result": "gated", "step": "fill_info", "reason": "low_confidence"}
    row = store.get("acme#1")
    assert row["status"] == "needs_human"
    assert row["pipeline_step"] == "fill_info"
    assert row["gate_reason"] == "low_confidence"


def test_resume_continues_from_the_gated_step(tables):
    store = _seed(tables)
    calls = {"fill": 0}

    def fill(pk, row, d):
        calls["fill"] += 1
        # first pass gates; after the human answers, the bank has it -> advance
        if d.answer_bank.lookup("Why us?", "Acme"):
            return advance(Status.TAILORED)
        return gate(GateReason.LOW_CONFIDENCE, "Why us?", question="Why us?")

    steps = {
        Step.ENSURE_ACCOUNT: lambda pk, row, d: advance(Status.FOUND),
        Step.PROCESS_JD: lambda pk, row, d: advance(Status.FOUND, match_score=8),
        Step.TWEAK_RESUME: lambda pk, row, d: advance(Status.TAILORED),
        Step.FILL_INFO: fill,
        Step.SUBMIT: lambda pk, row, d: terminal(Status.APPLIED, confirmation_id="OK-2"),
    }
    deps = _deps(tables, steps)

    assert run("acme#1", deps)["result"] == "gated"
    assert store.get("acme#1")["status"] == "needs_human"

    # human answers from the website
    out = resume("acme#1", "Because payments.", deps,
                 scope=AnswerScope.COMPANY, company="Acme")
    assert out == {"result": "terminal", "status": "applied"}
    assert store.get("acme#1")["confirmation_id"] == "OK-2"
    assert calls["fill"] == 2  # gated once, resumed once


def test_resume_on_non_gated_job_is_noop(tables):
    _seed(tables, pipeline_step="submit")  # status FOUND, not needs_human
    steps = {s: (lambda pk, row, d: advance(Status.FOUND)) for s in Step}
    out = resume("acme#1", "x", _deps(tables, steps))
    assert out["result"] == "not_gated"
