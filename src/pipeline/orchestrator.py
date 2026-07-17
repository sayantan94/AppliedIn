"""The Custom-Logic orchestrator: run the sequential steps, gate, and resume.

This is the human-in-the-loop core. `run` executes steps from wherever the job
currently sits until it hits a gate or a terminal state. A gate persists the
step it stopped at (`pipeline_step`) and sets `needs_human`; nothing runs until
the website calls `resume`, which banks the human's answer and re-runs from that
exact step — now able to proceed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core.logging import get_logger
from core.models import AnswerScope, GateReason, Status
from core.storage.answer_bank import AnswerBank
from core.storage.tracking import TrackingStore

from .steps import STEP_ORDER, Step, StepResult, next_step

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class PipelineDeps:
    tracking: TrackingStore
    answer_bank: AnswerBank
    # Step implementations: Step -> fn(pk, row, deps) -> StepResult.
    steps: dict[Step, Callable[[str, dict, PipelineDeps], StepResult]]
    notifier: Any | None = None  # .gate(pk, step, result) — dashboard/None
    now: Callable[[], str] = _now
    extra: dict = field(default_factory=dict)  # step-specific injected collaborators


def _append_event(deps: PipelineDeps, row: dict, step: Step, note: str) -> list[dict]:
    events = list(row.get("events") or [])
    events.append({"step": step.value, "at": deps.now(), "detail": note})
    return events


def run(pk: str, deps: PipelineDeps) -> dict:
    """Execute steps from the job's current position to a gate or terminal state."""
    row = deps.tracking.get(pk)
    if row is None:
        return {"result": "missing", "pk": pk}

    step = Step(row.get("pipeline_step") or STEP_ORDER[0].value)
    while True:
        fn = deps.steps[step]
        result = fn(pk, row, deps)
        events = _append_event(deps, row, step,
                               result.prompt or f"{result.kind}:{result.status or result.gate_reason}")

        if result.kind == "gate":
            deps.tracking.set_status(
                pk, Status.NEEDS_HUMAN,
                gate_reason=result.gate_reason.value,
                pipeline_step=step.value,
                gate_prompt=result.prompt,
                gate_pending=result.pending,
                events=events,
                **result.attrs,
            )
            if deps.notifier:
                deps.notifier.gate(pk, step, result)
            log.info("gated pk=%s step=%s reason=%s", pk, step.value, result.gate_reason.value)
            return {"result": "gated", "step": step.value, "reason": result.gate_reason.value}

        if result.kind == "terminal":
            deps.tracking.set_status(pk, result.status, pipeline_step=step.value,
                                     events=events, **result.attrs)
            log.info("terminal pk=%s status=%s", pk, result.status.value)
            return {"result": "terminal", "status": result.status.value}

        # advance
        nxt = next_step(step)
        if nxt is None:
            deps.tracking.set_status(pk, result.status or Status.APPLIED,
                                     pipeline_step=step.value, events=events, **result.attrs)
            return {"result": "done", "status": (result.status or Status.APPLIED).value}
        deps.tracking.set_status(pk, result.status, pipeline_step=nxt.value,
                                 events=events, **result.attrs)
        row = deps.tracking.get(pk)
        step = nxt


def resume(pk: str, answer: str, deps: PipelineDeps, *,
           scope: AnswerScope = AnswerScope.COMPANY,
           company: str | None = None,
           question: str | None = None) -> dict:
    """Human replied from the website: bank the answer and continue from the gate."""
    row = deps.tracking.get(pk)
    if row is None:
        return {"result": "missing", "pk": pk}
    if row.get("status") != Status.NEEDS_HUMAN.value:
        return {"result": "not_gated", "status": row.get("status")}

    if question is None:
        question = (row.get("gate_pending") or {}).get("question")
    if question:
        deps.answer_bank.put(question, answer, scope,
                             company=company or row.get("company"), source="dashboard")

    # clear the gate; run() will re-enter the persisted (gated) step, which can
    # now resolve because the answer is in the bank.
    deps.tracking.set_status(pk, Status.FOUND, gate_reason="", gate_prompt="")
    # keep pipeline_step where it gated
    deps.tracking.set_status(pk, Status.FOUND, pipeline_step=row.get("pipeline_step"))
    return run(pk, deps)


def resume_button(pk: str, action: str, deps: PipelineDeps) -> dict:
    """Non-text gate actions from the UI: approve (re-run), skip (terminal)."""
    if action == "skip":
        deps.tracking.set_status(pk, Status.SKIPPED, skip_reason="user_skipped")
        return {"result": "skipped"}
    row = deps.tracking.get(pk)
    if row:
        deps.tracking.set_status(pk, Status.FOUND, gate_reason="")
    return run(pk, deps)


def gate_reasons() -> list[str]:
    return [r.value for r in GateReason]
