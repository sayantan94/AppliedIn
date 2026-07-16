"""Async worker behind the webhook queue + the gate-answer resume path.

``process`` consumes one raw update (as enqueued by ``webhook.handler``),
routes it, and sends the reply back over WhatsApp when a client is wired.

``resume_gate`` implements the HLD approval-resume contract: the approved
answer is merged into the persisted field-map JSON and banked at the approved
scope FIRST, then a fresh apply task is enqueued — the worker re-loads the
live form, structure-diffs it against the gate-time snapshot, and re-fills
from the persisted field map (no new LLM mapping).
"""

from __future__ import annotations

import json

from appliedin_core.logging import get_logger
from appliedin_core.models import AnswerScope, Status
from botocore.exceptions import ClientError

from .commands import Deps, extract_message, route

log = get_logger(__name__)

FIELDMAP_PREFIX = "fieldmaps"


def process(update: dict, *, deps: Deps) -> str:
    """Route one inbound update and (if wired) send the reply back."""
    reply = route(update, deps=deps)
    msg = extract_message(update)
    if reply and deps.client is not None and msg and msg.get("wa_id"):
        deps.client.send_text(msg["wa_id"], reply)
    return reply


def _fieldmap_key(row: dict, pk: str) -> str:
    return row.get("fieldmap_key") or f"{FIELDMAP_PREFIX}/{pk}.json"


def _load_fieldmap(deps: Deps, key: str) -> dict:
    try:
        return json.loads(deps.artifacts.get(key))
    except ClientError:  # no fieldmap persisted yet (e.g. gated_mode approval)
        return {}


def resume_gate(
    pk: str,
    answer: str,
    scope: AnswerScope | str,
    company: str | None = None,
    *,
    deps: Deps,
) -> None:
    """Merge an approved gate answer and auto-resume the application.

    Order matters (HLD): field map + answer bank are updated BEFORE the fresh
    apply task is enqueued, so the re-fill always sees the approved answer.
    """
    scope = AnswerScope(scope) if isinstance(scope, str) else scope
    row = deps.tracking.get(pk) or {}
    company = company or row.get("company")
    question = row.get("gate_question", "")

    if question and answer:
        key = _fieldmap_key(row, pk)
        fieldmap = _load_fieldmap(deps, key)
        fieldmap[question] = answer
        deps.artifacts.put(
            FIELDMAP_PREFIX,
            key.removeprefix(f"{FIELDMAP_PREFIX}/"),
            json.dumps(fieldmap).encode(),
            "application/json",
        )
        deps.bank.put(question, answer, scope, company=company, source="gate_reply")
        log.info("gate answer banked for %s at scope=%s", pk, scope.value)

    # Back to ready-for-apply; the worker's resume path owns the structure diff.
    deps.tracking.set_status(pk, Status.TAILORED, gate_reason="", resumed_via="whatsapp")
    deps.queue.enqueue(deps.apply_queue_url, {"pk": pk})
