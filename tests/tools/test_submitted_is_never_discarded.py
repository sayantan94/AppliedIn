"""A submission that happened must never be turned into "nothing was sent".

Each case here is a way a job that HAD been applied to was handed back as
untouched, failed or retryable. Every one of those ends the same way: a second
application under a real name. They were found by reading the code path, not in
production, which is the cheap way to find them.
"""
import asyncio
import json

from tools import claude_chrome as cc
from tools.claude_chrome import classify, guard_value


class _Proc:
    """Stands in for the `claude --chrome` subprocess. No browser, no network."""
    pid = 4242
    returncode = 0

    def __init__(self, stdout: str) -> None:
        self._out = stdout.encode()

    async def communicate(self):
        return self._out, b""


def _run(monkeypatch, reply: str) -> tuple[dict, str]:
    async def fake_exec(*_a, **_kw):
        return _Proc(json.dumps({"result": reply}))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return asyncio.run(cc._run_task_impl("apply", report_key="outcome", kind="apply"))


APPLIED = '{"outcome": "applied", "confirmation": "Thank you for applying to Acme"}'


def test_a_recovered_click_error_does_not_discard_the_submission(monkeypatch):
    # The task tells the session to recover from a refused click and carry on. A
    # session that did, submitted, and then MENTIONED the error used to match the
    # conflict marker in its own reply: the report was dropped, the job was
    # re-queued as untouched, and it was applied to again.
    report, problem = _run(monkeypatch, (
        "One click failed with 'Cannot access a chrome-extension:// URL of "
        "different extension'; I switched tabs, finished and submitted.\n" + APPLIED))
    assert problem == ""
    assert classify(report)["status"] == "applied"


def test_a_conflict_with_no_submission_is_still_a_conflict(monkeypatch):
    # The other half: the override is for a claimed submission only. A session
    # that never got past the refused click is still an environment fault.
    report, problem = _run(monkeypatch, (
        "Every click failed: Cannot access a chrome-extension:// URL of different "
        'extension.\n{"outcome": "blocked", "detail": "could not click"}'))
    assert report == {} and problem == cc.CONFLICT_MESSAGE


def test_a_confirmation_that_says_there_was_none_is_not_a_confirmation():
    # "confirmation" is free text the session writes. A non-empty account of the
    # page NOT confirming was being stored as the confirmation.
    for said in ("No confirmation message appeared; the page stayed on the form",
                 "N/A", "Unable to capture one", "The page did not show a confirmation"):
        out = classify({"outcome": "applied", "confirmation": said})
        assert out["status"] == "uncertain", said
    # Real confirmation wording that merely contains "no" still counts.
    for said in ("Thank you. No further action is needed", "Application Success"):
        assert classify({"outcome": "applied", "confirmation": said})["status"] == "applied"


def test_a_declaration_is_caught_wherever_it_sits():
    # The label-only check passed all three: the declaration is in the VALUE on
    # most real EEO forms, or split across the question and a bare "Yes".
    for filled in (
        {"Disability status": "Yes, I have a disability, or have had one in the past"},
        {"Veteran status": "I identify as one or more of the classifications of "
                           "protected veteran"},
        {"Are you Hispanic or Latino?": "Yes"},
    ):
        out = classify({"outcome": "applied", "confirmation": "Thanks!", "filled": filled})
        assert (out["status"], out["reason"]) == ("failed", "guardrail"), filled
        # It may well have gone out. "Failed" alone invites a Retry.
        assert "check the portal" in out["detail"]


def test_declining_and_acknowledging_are_not_declarations():
    # Negatives must go through or a required field is left blank. And ticking
    # "I have read the EEO statement" declares nothing: reading it as an
    # affirmation recorded a submitted application as failed.
    for filled in (
        {"Disability status": "No, I do not have a disability"},
        {"Veteran status": "I am not a protected veteran"},
        {"Are you Hispanic or Latino?": "No"},
        {"Race": "Decline to self-identify"},
        {"I have read the Equal Opportunity statement covering race, disability "
         "and veteran status": "checked"},
    ):
        out = classify({"outcome": "applied", "confirmation": "Thanks!", "filled": filled})
        assert out["status"] == "applied", filled


def test_a_sanctions_question_answered_yes_is_never_recorded_as_applied():
    q = "Are you a citizen of or located in Cuba, Iran, North Korea or Syria?"
    out = classify({"outcome": "applied", "confirmation": "Thanks!", "filled": {q: "Yes"}})
    assert out["status"] == "failed" and out["reason"] == "guardrail"
    ok = classify({"outcome": "applied", "confirmation": "Thanks!", "filled": {q: "No"}})
    assert ok["status"] == "applied"


def test_incubator_is_not_cuba():
    # The pattern had no word boundary, so an essay question was "a sanctions
    # question" and its answer was replaced with "None of the above".
    assert guard_value("Describe your time at a startup incubator",
                       "Led platform work") == ("Led platform work", None)
