"""Default step implementations — one agent per step.

Each function is the seam to a building block already in the repo. The heavy
work (LLM calls, browser via AgentCore, Playwright) is reached through
`deps.extra[...]` callables so this module imports nothing heavy and stays
unit-testable; the runner wires the real implementations in.

  ensure_account  -> worker.signup.ensure_account          (+ AgentCore Browser)
  process_jd      -> discovery/tailoring scoring agent      (Strands + Bedrock)
  tweak_resume    -> tailoring.tailor + truthfulness + render (Strands + critic)
  fill_info       -> worker.confidence + worker.engines      (scripted / ReAct)
  submit          -> worker.apply submit path                (+ AgentCore Browser)
"""

from __future__ import annotations

from core.models import GateReason, Status

from .orchestrator import PipelineDeps
from .steps import Step, StepResult, advance, gate, terminal

MIN_MATCH_SCORE = 7


def step_ensure_account(pk: str, row: dict, deps: PipelineDeps) -> StepResult:
    ensure = deps.extra["ensure_account"]
    try:
        ensure(row)  # returns creds; existing account is a no-op
    except deps.extra["AccountBlocked"]:
        return gate(GateReason.NO_ACCOUNT,
                    "Auto-signup was blocked (CAPTCHA/2FA). Create the account, then retry.")
    return advance(Status.FOUND)


def step_process_jd(pk: str, row: dict, deps: PipelineDeps) -> StepResult:
    score, matched = deps.extra["score_jd"](row)  # agentic extract + match
    if score < deps.extra.get("min_score", MIN_MATCH_SCORE):
        return terminal(Status.SKIPPED, match_score=score,
                        skip_reason=f"score {score} < threshold")
    return advance(Status.FOUND, match_score=score, matched_prefs=matched)


def step_tweak_resume(pk: str, row: dict, deps: PipelineDeps) -> StepResult:
    result = deps.extra["tailor_render"](row)  # tailor + critic loop + validate + render
    if not result.get("truthful", True):
        return gate(GateReason.LOW_CONFIDENCE,
                    "Tailored résumé failed the truthfulness check — review the diff.",
                    diff=result.get("diff"))
    return advance(Status.TAILORED,
                   resume_version=result["resume_version"],
                   resume_s3_key=result["resume_s3_key"])


def step_fill_info(pk: str, row: dict, deps: PipelineDeps) -> StepResult:
    fill = deps.extra["resolve_fields"](row, deps.answer_bank)
    if not fill["all_high_confidence"]:
        q = fill["pending"]["question"]
        return gate(GateReason.LOW_CONFIDENCE,
                    f"Need your answer: {q}",
                    question=q, draft=fill["pending"].get("draft"),
                    scope=fill["pending"].get("scope", "company"))
    return advance(Status.TAILORED, fieldmap_s3_key=fill["fieldmap_s3_key"])


def step_submit(pk: str, row: dict, deps: PipelineDeps) -> StepResult:
    # atomic daily-cap reservation before the submit click
    if not deps.extra["reserve_cap"]():
        return terminal(Status.CAPPED)
    try:
        res = deps.extra["submit"](row)  # sets submitting, clicks, captures proof
    except deps.extra["CaptchaBlocked"]:
        return gate(GateReason.CAPTCHA, "A CAPTCHA blocked submission — apply manually, then /done.")
    except deps.extra["JobGone"]:
        return terminal(Status.JOB_GONE)
    return terminal(Status.APPLIED,
                    confirmation_id=res["confirmation_id"],
                    screenshot_s3_key=res.get("screenshot_s3_key"),
                    submitted_at=deps.now())


def default_steps() -> dict[Step, object]:
    return {
        Step.ENSURE_ACCOUNT: step_ensure_account,
        Step.PROCESS_JD: step_process_jd,
        Step.TWEAK_RESUME: step_tweak_resume,
        Step.FILL_INFO: step_fill_info,
        Step.SUBMIT: step_submit,
    }
