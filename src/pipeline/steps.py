"""The steps of the per-application pipeline, and the result each step returns.

Each Step is one agent's job. A step function returns a StepResult telling the
orchestrator what to do next: ADVANCE to the next step, GATE (stop and ask the
human), or reach a TERMINAL state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from core.models import GateReason, Status


class Step(str, Enum):
    ENSURE_ACCOUNT = "ensure_account"
    PROCESS_JD = "process_jd"
    TWEAK_RESUME = "tweak_resume"
    FILL_INFO = "fill_info"
    SUBMIT = "submit"


STEP_ORDER: list[Step] = [
    Step.ENSURE_ACCOUNT,
    Step.PROCESS_JD,
    Step.TWEAK_RESUME,
    Step.FILL_INFO,
    Step.SUBMIT,
]

# What each agent does — the human-readable map (also surfaced to the dashboard).
STEP_INFO: dict[Step, str] = {
    Step.ENSURE_ACCOUNT: "Ensure a portal account exists (auto-signup; gate on CAPTCHA/2FA).",
    Step.PROCESS_JD: "Agentic: extract the role, LLM match-score it against profile + "
    "preferences; below threshold → skipped.",
    Step.TWEAK_RESUME: "Tailor résumé emphasis-only with a critic/refine loop, run the "
    "truthfulness validator, render the PDF.",
    Step.FILL_INFO: "Resolve every form field via the answer bank + confidence gate "
    "(scripted per-ATS, or a ReAct sub-agent for custom portals).",
    Step.SUBMIT: "Atomic daily-cap check, submit, capture confirmation + screenshot.",
}


@dataclass
class StepResult:
    kind: str  # 'advance' | 'gate' | 'terminal'
    status: Status | None = None
    gate_reason: GateReason | None = None
    prompt: str = ""
    attrs: dict = field(default_factory=dict)  # row attributes to persist
    pending: dict = field(default_factory=dict)  # gate context: question/draft/scope


def advance(status: Status, **attrs) -> StepResult:
    """Step succeeded; move to the next step, persisting `status` + attrs."""
    return StepResult("advance", status=status, attrs=attrs)


def gate(reason: GateReason, prompt: str = "", **pending) -> StepResult:
    """Step is stuck; stop here and ask the human. Resumes from THIS step."""
    return StepResult("gate", gate_reason=reason, prompt=prompt, pending=pending)


def terminal(status: Status, **attrs) -> StepResult:
    """Reach a terminal state (applied / skipped / job_gone / error)."""
    return StepResult("terminal", status=status, attrs=attrs)


def next_step(step: Step) -> Step | None:
    i = STEP_ORDER.index(step)
    return STEP_ORDER[i + 1] if i + 1 < len(STEP_ORDER) else None
