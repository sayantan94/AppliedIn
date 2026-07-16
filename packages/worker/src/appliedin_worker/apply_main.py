"""Apply orchestration — one job per Fargate task (HLD AUTO/GATE paths).

The branch matrix, in order:
  1. redelivery of a job already ``submitting``  -> needs_human(submit_uncertain),
     NEVER re-submit (the click may have landed before a crash)
  2. account wall + blocked signup               -> gate(no_account)
  3. any low-confidence field                    -> gate(low_confidence)
  4. portal mode != auto                         -> gate(gated_mode), fieldmap
     persisted so approval is one tap
  5. daily cap exhausted                         -> status ``capped`` (parked;
     the discovery cron re-enqueues it — NOT a human gate, per HLD guardrail 1)
  6. AUTO: reserve cap slot -> ``submitting`` -> submit -> confirmation +
     screenshot -> ``applied`` + receipt; a submit that yields no confirmation
     gates as submit_uncertain.

All collaborators arrive via :class:`ApplyDeps` so tests inject fakes — no
browser, network, or model required.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from appliedin_core.config import Settings
from appliedin_core.logging import get_logger
from appliedin_core.models import ApplyMode, GateReason, JobRecord, Status
from appliedin_core.storage.answer_bank import AnswerBank
from appliedin_core.storage.artifacts import ArtifactStore
from appliedin_core.storage.secrets import SecretsClient
from appliedin_core.storage.tracking import TrackingStore

from .confidence import resolve_field
from .engines import FillEngine, pick_engine
from .gates import Notifier, raise_gate
from .signup import SignupError, ensure_account

log = get_logger(__name__)

# Async submitter: clicks submit and returns the portal confirmation id, or
# None when no confirmation could be extracted (-> submit_uncertain gate).
Submitter = Callable[[Any], Awaitable[str | None]]


@dataclass
class PortalConfig:
    """Per-company portal settings from watchlist.yaml. Default mode is GATED:
    every new portal earns auto through burn-in (HLD guardrail 2)."""

    mode: ApplyMode = ApplyMode.GATED
    login_secret: str = ""


@dataclass
class ApplyDeps:
    """Injected collaborators for one apply run."""

    tracking: TrackingStore
    answer_bank: AnswerBank
    artifacts: ArtifactStore
    secrets: SecretsClient
    notifier: Notifier
    settings: Settings
    identity: dict = field(default_factory=dict)  # signup identity (global facts)
    portals: dict[str, PortalConfig] = field(default_factory=dict)  # lowercased company -> cfg
    page: Any = None  # injected in tests; None -> real Playwright launch
    engine: FillEngine | None = None  # override; None -> pick_engine(ats)
    gmail_fetch: Callable[[str], str | None] | None = None
    submitter: Submitter | None = None


async def run_apply(pk: str, *, deps: ApplyDeps) -> Status | None:
    """Apply to one job. Returns the final Status written (None if unknown pk)."""
    row = deps.tracking.get(pk)
    if row is None:
        log.warning("run_apply: unknown pk %s", pk)
        return None

    def gate(reason: GateReason, fieldmap: dict, snapshot: dict, shots: list[bytes]) -> Status:
        raise_gate(
            pk,
            reason,
            fieldmap,
            snapshot,
            shots,
            tracking=deps.tracking,
            artifacts=deps.artifacts,
            notifier=deps.notifier,
        )
        return Status.NEEDS_HUMAN

    # Idempotency (HLD AUTO path): a job already `submitting` on entry means a
    # previous task crashed around the submit click. The submit may have
    # landed — verify on the portal, never re-submit.
    if row.get("status") == Status.SUBMITTING.value:
        return gate(GateReason.SUBMIT_UNCERTAIN, {}, {}, [])

    company = str(row.get("company", ""))
    portal = deps.portals.get(company.lower(), PortalConfig())
    page = deps.page
    if page is None:  # pragma: no cover - real browser only in the container
        from .browser import launch_page

        page = await launch_page()

    await page.goto(row["jd_url"])

    if portal.login_secret:
        try:
            await ensure_account(
                portal.login_secret,
                deps.identity,
                deps.secrets,
                page=page,
                gmail_fetch=deps.gmail_fetch,
            )
        except SignupError:
            log.exception("auto-signup blocked for %s", pk)
            return gate(GateReason.NO_ACCOUNT, {}, {}, await _screenshots(page))

    job = JobRecord(
        company=company,
        job_id=str(row.get("job_id", "")),
        title=str(row.get("title", "")),
        jd_url=str(row.get("jd_url", "")),
        jd_text="",
        location=str(row.get("location", "")),
        ats=str(row.get("ats", "")),
    )
    engine = deps.engine if deps.engine is not None else pick_engine(job.ats)

    def resolver(label: str):
        return resolve_field(label, job.ats, deps.answer_bank, job.company)

    result = await engine.fill(page, job, resolver)
    shots = await _screenshots(page)

    # One low-confidence field gates the whole application (HLD guardrail 3).
    if result.low_confidence_labels:
        return gate(GateReason.LOW_CONFIDENCE, result.fields, result.form_snapshot, shots)

    if portal.mode is not ApplyMode.AUTO:
        return gate(GateReason.GATED_MODE, result.fields, result.form_snapshot, shots)

    # AUTO path. Reserve a cap slot atomically, immediately before `submitting`.
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    if not deps.tracking.try_increment_daily_cap(today, deps.settings.daily_cap):
        deps.tracking.set_status(pk, Status.CAPPED)
        log.info("daily cap reached; parked %s as capped", pk)
        return Status.CAPPED

    deps.tracking.set_status(pk, Status.SUBMITTING)
    submitter = deps.submitter if deps.submitter is not None else _default_submit
    try:
        confirmation = await submitter(page)
    except Exception:
        log.exception("submit failed for %s", pk)
        confirmation = None
    if confirmation is None:
        # The click may or may not have landed; a human must verify on the
        # portal's My Applications page. Never auto-retried (status was
        # `submitting`, so redelivery hits the idempotency branch).
        return gate(
            GateReason.SUBMIT_UNCERTAIN,
            result.fields,
            result.form_snapshot,
            await _screenshots(page),
        )

    final_shots = await _screenshots(page)
    screenshot_key = ""
    if final_shots:
        screenshot_key = deps.artifacts.put(
            "screenshots", f"{pk}/confirmation.png", final_shots[0], "image/png"
        )
    deps.tracking.set_status(
        pk,
        Status.APPLIED,
        confirmation_id=confirmation,
        screenshot_s3_key=screenshot_key,
        submitted_at=datetime.now(UTC).isoformat(),
    )
    deps.notifier.send_receipt(pk, confirmation)
    log.info("applied %s confirmation=%s", pk, confirmation)
    return Status.APPLIED


async def _screenshots(page: Any) -> list[bytes]:
    """Best-effort page screenshot (fakes may not implement it)."""
    shot_fn = getattr(page, "screenshot", None)
    if shot_fn is None:
        return []
    try:
        return [await shot_fn()]
    except Exception:  # never let evidence capture kill the run
        log.exception("screenshot failed")
        return []


async def _default_submit(page: Any) -> str | None:
    """Conservative default: click submit, extract nothing. Returning None
    routes to submit_uncertain — per-ATS confirmation extractors replace this
    as portals are burned in."""
    await page.click('button[type="submit"]')
    return None
