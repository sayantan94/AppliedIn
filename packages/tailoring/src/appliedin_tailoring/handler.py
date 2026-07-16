"""Tailoring Lambda — SQS consumer for the tailor-queue.

Per record: load the tracking row -> stage-2 LLM match score (< threshold ->
``skipped``) -> tailoring agent -> deterministic truthfulness validator
(violations -> ``needs_human`` with the diff, never enqueued) -> Typst PDF ->
S3 -> drafted answers -> assist-mode branch (gate to human) -> else
``tailored`` + enqueue to the apply-queue.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from appliedin_core.config import get_settings
from appliedin_core.logging import get_logger
from appliedin_core.models import ApplyMode, GateReason, Status
from appliedin_core.storage.artifacts import ArtifactStore
from appliedin_core.storage.queue import Queue
from appliedin_core.storage.tracking import TrackingStore

from .render import render_pdf
from .scoring import score_match
from .tailor import tailor
from .truthfulness import validate

log = get_logger(__name__)

#: Fallback stage-2 threshold when preferences.yaml does not set one.
DEFAULT_MIN_MATCH_SCORE = 7


def load_base_resume(path: str | Path) -> dict:
    """Load ``resume/base.yaml`` — the single source of truth resume."""
    return yaml.safe_load(Path(path).read_text()) or {}


def load_min_match_score(path: str | Path) -> int:
    """Read ``min_match_score`` from preferences.yaml (default 7)."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    return int(data.get("min_match_score", DEFAULT_MIN_MATCH_SCORE))


def load_company_modes(path: str | Path) -> dict[str, ApplyMode]:
    """Map lowercased company name -> ApplyMode from watchlist.yaml."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    return {
        str(c["name"]).lower(): ApplyMode(c.get("mode", ApplyMode.GATED.value))
        for c in data.get("companies", [])
    }


def draft_answers(base: dict, jd_text: str, row: dict) -> dict[str, str]:
    """Draft the standard application answers (why-us, salary, visa, notice).

    P0 stub: returns an empty mapping. This is the single extension point for
    drafted answers + the optional cover letter (``requires_cover_letter``);
    the apply worker reads ``row["drafted_answers"]`` uniformly either way.
    """
    return {}


def _jd_text(row: dict, artifacts: ArtifactStore) -> str:
    """Best available JD text: tracking row, else the S3 jd/ snapshot, else title."""
    if text := row.get("jd_text"):
        return str(text)
    try:
        return artifacts.get(f"jd/{row['pk']}.txt").decode("utf-8")
    except Exception:  # no snapshot stored — degrade to the title
        return str(row.get("title", ""))


def process_record(
    pk: str,
    *,
    tracking: TrackingStore,
    artifacts: ArtifactStore,
    queue: Queue,
    base: dict,
    min_match_score: int,
    modes: dict[str, ApplyMode],
    apply_queue_url: str,
) -> str:
    """Run the tailoring pipeline for one job; returns the terminal status."""
    row = tracking.get(pk)
    if row is None:
        log.warning("no tracking row for %s — dropping message", pk)
        return "missing"

    jd_text = _jd_text(row, artifacts)

    score = score_match(jd_text, base)
    if score < min_match_score:
        tracking.set_status(
            pk,
            Status.SKIPPED,
            match_score=score,
            skip_reason=f"match score {score} < {min_match_score}",
        )
        return Status.SKIPPED.value

    tailored = tailor(base, jd_text)

    violations = validate(base, tailored)
    if violations:
        # Guardrail 4: structural-fact mismatch never proceeds. Store the diff
        # for the WhatsApp gate message; no gate_reason (this is not a form
        # gate) and never enqueue.
        tracking.set_status(
            pk,
            Status.NEEDS_HUMAN,
            match_score=score,
            truthfulness_diff=violations,
        )
        return Status.NEEDS_HUMAN.value

    pdf = render_pdf(tailored)
    version = int(row.get("resume_version") or 0) + 1
    resume_key = artifacts.put(
        "resumes", f"{pk}-{version}.pdf", pdf, "application/pdf"
    )
    answers = draft_answers(base, jd_text, row)

    mode = modes.get(str(row.get("company", "")).lower(), ApplyMode.GATED)
    if mode is ApplyMode.ASSIST:
        # Notify-and-assist: everything is prepared, a human drives the submit.
        # TODO(whatsapp): send the assist notification (resume link + drafted
        # answers) via packages/whatsapp once the Meta client lands (Phase 6).
        tracking.set_status(
            pk,
            Status.NEEDS_HUMAN,
            gate_reason=GateReason.GATED_MODE.value,
            match_score=score,
            resume_s3_key=resume_key,
            resume_version=version,
            drafted_answers=answers,
        )
        return Status.NEEDS_HUMAN.value

    tracking.set_status(
        pk,
        Status.TAILORED,
        match_score=score,
        resume_s3_key=resume_key,
        resume_version=version,
        drafted_answers=answers,
    )
    queue.enqueue(apply_queue_url, {"pk": pk})
    return Status.TAILORED.value


def handler(event, context):  # noqa: ANN001 - Lambda signature
    settings = get_settings()
    tracking = TrackingStore(settings.applications_table, region=settings.aws_region)
    artifacts = ArtifactStore(settings.artifacts_bucket, region=settings.aws_region)
    queue = Queue(region=settings.aws_region)

    config_dir = Path(settings.config_dir)
    base = load_base_resume(config_dir.parent / "resume" / "base.yaml")
    min_score = load_min_match_score(config_dir / "preferences.yaml")
    modes = load_company_modes(config_dir / "watchlist.yaml")

    results: dict[str, str] = {}
    for record in event.get("Records", []):
        pk = json.loads(record["body"])["pk"]
        try:
            results[pk] = process_record(
                pk,
                tracking=tracking,
                artifacts=artifacts,
                queue=queue,
                base=base,
                min_match_score=min_score,
                modes=modes,
                apply_queue_url=settings.apply_queue_url,
            )
        except Exception:
            # One bad job must not poison the batch; park it as error (max-2
            # attempts / never-hammer ethos — no blind redelivery loop).
            log.exception("tailoring failed for %s", pk)
            tracking.set_status(pk, Status.ERROR, error="tailoring pipeline error")
            results[pk] = Status.ERROR.value
    return results
