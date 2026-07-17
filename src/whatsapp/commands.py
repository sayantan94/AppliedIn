"""Inbound routing: slash commands, gate-button taps, gate replies, free-text Q&A.

Routing rules (HLD "WhatsApp Bot" block):
  - ``/pause /resume /status /skip <id> /done <id> /fact <q> = <a>`` — commands.
  - Button taps carry a deterministic slug id (``templates.button_id``); the
    target job is resolved from the tap's reply-context (``gate_msg_id`` stored
    on the row) or, failing that, the single open gate.
  - Free text that *replies to* an open unknown_field/low_confidence gate is a
    gate answer -> merge + auto-resume (processor.resume_gate).
  - Any other free text goes to the strictly read-only Q&A agent — it can
    never change state.

Kill switch: ``/pause`` writes a flag item at pk ``meta#killswitch`` in the
applications table (attribute ``paused``). ``is_paused()`` is the read side;
the apply worker checks it before submitting. ``meta#*`` rows are filtered
out of ``/status`` counts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.models import AnswerScope, GateReason, Status
from core.storage.answer_bank import AnswerBank
from core.storage.artifacts import ArtifactStore
from core.storage.queue import Queue
from core.storage.tracking import TrackingStore

from . import templates as t

KILLSWITCH_PK = "meta#killswitch"

_ANSWER_GATES = (GateReason.UNKNOWN_FIELD.value, GateReason.LOW_CONFIDENCE.value)
_APPROVE_WORDS = ("ok", "yes", "approve", "approved")

HELP = (
    "Commands: /pause /resume /status /skip <id> /done <id> /fact <question> = <answer>. "
    "Anything else is a question for the read-only Q&A agent."
)


@dataclass
class Deps:
    """Everything the router touches — injectable so tests use fakes/moto."""

    tracking: TrackingStore
    bank: AnswerBank
    artifacts: ArtifactStore
    queue: Queue | Any
    apply_queue_url: str = ""
    client: Any = None  # MetaClient (outbound replies); optional
    qa: Callable[[str], str] | None = None  # injectable Q&A; default = qa_agent.answer
    model: Any = None  # optional LLM override for the Q&A agent


# --------------------------------------------------------------------------- inbound parsing

def extract_message(update: dict) -> dict | None:
    """Normalize a raw Meta update (or an already-simple dict) to one message.

    Returns ``{"wa_id", "text", "button_id", "button_title", "context_id"}``
    for the first message in the update, or None if there is none.
    """
    for entry in update.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                out = {
                    "wa_id": msg.get("from", ""),
                    "text": None,
                    "button_id": None,
                    "button_title": None,
                    "context_id": (msg.get("context") or {}).get("id"),
                }
                if msg.get("type") == "text":
                    out["text"] = msg.get("text", {}).get("body", "")
                elif msg.get("type") == "interactive":
                    reply = msg.get("interactive", {}).get("button_reply", {})
                    out["button_id"] = reply.get("id")
                    out["button_title"] = reply.get("title")
                return out
    if "text" in update or "button_id" in update:  # simplified form (tests, processor)
        return {
            "wa_id": update.get("wa_id", ""),
            "text": update.get("text"),
            "button_id": update.get("button_id"),
            "button_title": update.get("button_title"),
            "context_id": update.get("context_id"),
        }
    return None


# --------------------------------------------------------------------------- kill switch

def set_paused(tracking: TrackingStore, paused: bool) -> None:
    # Reuses set_status for the flag item; SKIPPED is inert and meta# rows are
    # excluded from /status counts, so the GSI row is harmless.
    tracking.set_status(KILLSWITCH_PK, Status.SKIPPED, paused=paused)


def is_paused(tracking: TrackingStore) -> bool:
    row = tracking.get(KILLSWITCH_PK)
    return bool(row and row.get("paused"))


# --------------------------------------------------------------------------- gate resolution

def _open_gates(deps: Deps) -> list[dict]:
    return [
        r for r in deps.tracking.query_status(Status.NEEDS_HUMAN)
        if not str(r.get("pk", "")).startswith("meta#")
    ]


def _resolve_gate_row(msg: dict, deps: Deps) -> dict | None:
    """Find the gate a tap/reply refers to: reply-context first, else the only open gate."""
    gates = _open_gates(deps)
    ctx = msg.get("context_id")
    if ctx:
        for row in gates:
            if row.get("gate_msg_id") == ctx:
                return row
        return None
    if len(gates) == 1:
        return gates[0]
    return None


# --------------------------------------------------------------------------- route

def route(update: dict, *, deps: Deps) -> str:
    """Dispatch one inbound update; returns the reply text."""
    msg = extract_message(update)
    if msg is None:
        return ""

    if msg.get("button_id"):
        return _handle_button(msg, deps)

    text = (msg.get("text") or "").strip()
    if not text:
        return ""
    if text.startswith("/"):
        return _handle_command(text, deps)

    # Free text replying to an open answer-gate -> conversational gate answer.
    if msg.get("context_id"):
        row = _resolve_gate_row(msg, deps)
        if row is not None and row.get("gate_reason") in _ANSWER_GATES:
            return _handle_gate_answer(text, row, deps)

    # Anything else: read-only Q&A, anytime.
    return _qa(text, deps)


def _qa(text: str, deps: Deps) -> str:
    if deps.qa is not None:
        return deps.qa(text)
    from .qa_agent import answer  # lazy: pulls strands only when actually used

    return answer(text, tracking=deps.tracking, artifacts=deps.artifacts, model=deps.model)


# --------------------------------------------------------------------------- commands

def _handle_command(text: str, deps: Deps) -> str:
    cmd, _, rest = text.partition(" ")
    cmd, rest = cmd.lower(), rest.strip()

    if cmd == "/pause":
        set_paused(deps.tracking, True)
        return "Paused. No new submissions until /resume."
    if cmd == "/resume":
        set_paused(deps.tracking, False)
        return "Resumed. Submissions are live again."
    if cmd == "/status":
        return _status_summary(deps)
    if cmd == "/skip":
        return _set_job_status(rest, Status.SKIPPED, deps, verb="Skipped")
    if cmd == "/done":
        return _set_job_status(rest, Status.APPLIED_MANUAL, deps, verb="Marked applied (manual)")
    if cmd == "/fact":
        return _handle_fact(rest, deps)
    return HELP


def _set_job_status(pk: str, status: Status, deps: Deps, *, verb: str) -> str:
    if not pk:
        return f"Usage: {verb.split()[0].lower()} needs a job id (company#job_id)."
    if deps.tracking.get(pk) is None:
        return f"Unknown job {pk!r}."
    deps.tracking.set_status(pk, status, gate_reason="")
    return f"{verb}: {pk}."


def _handle_fact(rest: str, deps: Deps) -> str:
    question, sep, answer = rest.partition("=")
    question, answer = question.strip(), answer.strip()
    if not sep or not question or not answer:
        return "Usage: /fact <question> = <answer>"
    deps.bank.put(question, answer, AnswerScope.GLOBAL, source="whatsapp_fact")
    return f'Saved global fact: "{question}" -> "{answer}".'


def _status_summary(deps: Deps) -> str:
    parts = []
    for status in Status:
        rows = [
            r for r in deps.tracking.query_status(status)
            if not str(r.get("pk", "")).startswith("meta#")
        ]
        if rows:
            parts.append(f"{status.value}: {len(rows)}")
    summary = " | ".join(parts) if parts else "No tracked applications yet."
    if is_paused(deps.tracking):
        summary = "PAUSED. " + summary
    return summary


# --------------------------------------------------------------------------- buttons & gate answers

def _handle_button(msg: dict, deps: Deps) -> str:
    row = _resolve_gate_row(msg, deps)
    if row is None:
        return "I couldn't match that tap to an open gate — try /status."
    pk = row["pk"]
    action = msg["button_id"]

    if action == t.button_id(t.BTN_SKIP):
        deps.tracking.set_status(pk, Status.SKIPPED, gate_reason="")
        return f"Skipped: {pk}."
    if action == t.button_id(t.BTN_MANUAL):
        deps.tracking.set_status(pk, Status.NEEDS_HUMAN, manual=True)
        return f"All yours. Send /done {pk} once you've submitted."
    if action == t.button_id(t.BTN_RETRY):
        deps.queue.enqueue(deps.apply_queue_url, {"pk": pk})
        return f"Retrying {pk} (counts toward the 2-attempt limit)."
    if action == t.button_id(t.BTN_MARK_APPLIED):
        deps.tracking.set_status(pk, Status.APPLIED, gate_reason="")
        return f"Marked applied: {pk}."
    if action in (t.button_id(t.BTN_APPROVE), t.button_id(t.BTN_APPROVE_SUBMIT)):
        return _approve_gate(row, deps, scope=row.get("proposed_scope", "global"))
    if action == t.button_id(t.BTN_COMPANY_ONLY):
        # "company only" restricts a proposed global save to this company.
        return _approve_gate(row, deps, scope=AnswerScope.COMPANY.value)
    return HELP


def _approve_gate(row: dict, deps: Deps, *, scope: str) -> str:
    from .processor import resume_gate  # lazy: avoids a module-level import cycle

    pk = row["pk"]
    resume_gate(
        pk,
        row.get("drafted_answer", ""),
        scope,
        row.get("company"),
        deps=deps,
    )
    return f"Approved — resuming {pk}."


def _handle_gate_answer(text: str, row: dict, deps: Deps) -> str:
    """A free-text reply to an answer-gate: 'ok' approves the draft, else override."""
    from .processor import resume_gate  # lazy: avoids a module-level import cycle

    pk = row["pk"]
    answer = row.get("drafted_answer", "") if text.lower() in _APPROVE_WORDS else text
    resume_gate(pk, answer, row.get("proposed_scope", "global"), row.get("company"), deps=deps)
    return f'Got it — using "{answer}". Resuming {pk}.'
