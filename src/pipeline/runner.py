"""SQS consumer: for each queued job, run the pipeline.

Discovery enqueues `{pk}` after it extracts and stage-1 matches a role. This
handler drains that queue and runs the sequential agentic pipeline for each
job — advancing until the job reaches a terminal state or stops at a human gate.
"""

from __future__ import annotations

import json

from core.config import get_settings
from core.logging import get_logger
from core.storage.answer_bank import AnswerBank
from core.storage.tracking import TrackingStore

from .agents import default_steps
from .orchestrator import PipelineDeps, run

log = get_logger(__name__)


def build_deps() -> PipelineDeps:
    s = get_settings()
    return PipelineDeps(
        tracking=TrackingStore(s.applications_table, region=s.aws_region),
        answer_bank=AnswerBank(s.answer_bank_table, region=s.aws_region),
        steps=default_steps(),
        extra=_real_collaborators(),
    )


def _real_collaborators() -> dict:
    # Wired to the worker/tailoring implementations (and AgentCore Browser for
    # the browser steps). Imported lazily inside the runner so unit tests of the
    # orchestrator don't pull heavy deps.
    from worker import signup
    from worker.signup import SignupError, SignupVerificationError

    return {
        "ensure_account": lambda row: signup.ensure_account(row.get("login_secret", ""), row),
        "AccountBlocked": (SignupError, SignupVerificationError),
        # score_jd / tailor_render / resolve_fields / submit / reserve_cap are
        # bound here to the tailoring + worker functions during integration.
        "CaptchaBlocked": RuntimeError,
        "JobGone": RuntimeError,
    }


def handler(event, context):  # noqa: ANN001 - Lambda/SQS signature
    deps = build_deps()
    results = []
    for record in event.get("Records", []):
        pk = json.loads(record["body"])["pk"]
        try:
            results.append(run(pk, deps))
        except Exception:
            log.exception("pipeline failed for pk=%s", pk)
            results.append({"result": "error", "pk": pk})
    return {"processed": results}
