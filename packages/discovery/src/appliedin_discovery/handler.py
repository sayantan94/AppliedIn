"""Discovery Lambda — the fast, dumb poller.

Per cron tick: for each feed company, fetch -> stage-1 filter -> apply the
per-company watermark and first-run backfill cap -> conditional PutItem
(dedup) -> enqueue survivors to the tailor queue. Also re-enqueues ``capped``
rows to the apply queue so they retry on the next tick.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from appliedin_core.config import get_settings
from appliedin_core.logging import get_logger
from appliedin_core.models import DiscoveryMode, Status
from appliedin_core.storage.queue import Queue
from appliedin_core.storage.tracking import TrackingStore

from .adapters import ADAPTERS
from .filters import stage1_match
from .resolver import resolve
from .watchlist import CompanyConfig, Preferences, load_preferences, load_watchlist

log = get_logger(__name__)

BACKFILL_CAP = 25  # newest matching postings per company on the first poll


def _watermark_pk(company: str) -> str:
    return f"meta#watermark#{company.lower()}"


def resolve_company(company: CompanyConfig, client: httpx.Client) -> CompanyConfig:
    """Fill ats/board/discovery from careers_url when not explicitly set."""
    if not company.needs_resolution or not company.careers_url:
        return company
    match = resolve(company.careers_url, client)
    log.info(
        "resolved %s (%s) -> ats=%s discovery=%s",
        company.name, company.careers_url, match.ats, match.discovery.value,
    )
    return company.model_copy(
        update={"ats": match.ats, "board": match.board, "discovery": match.discovery}
    )


def discover_company(
    company: CompanyConfig,
    prefs: Preferences,
    tracking: TrackingStore,
    queue: Queue,
    client: httpx.Client,
    tailor_queue_url: str,
) -> int:
    """Discover one company; returns the number of jobs newly enqueued."""
    adapter = ADAPTERS.get(company.ats)
    if adapter is None:
        log.warning("no adapter for ats=%s (company=%s)", company.ats, company.name)
        return 0

    jobs = [j for j in adapter.fetch(company, client) if stage1_match(j, prefs)]

    wm_row = tracking.get(_watermark_pk(company.name))
    first_run = wm_row is None
    if first_run:
        jobs = jobs[:BACKFILL_CAP]

    enqueued = 0
    for job in jobs:
        if tracking.put_new(job):  # False => already seen, skip
            queue.enqueue(tailor_queue_url, {"pk": job.pk})
            enqueued += 1

    tracking.set_status(_watermark_pk(company.name), Status.FOUND, last_poll="done")
    return enqueued


def requeue_capped(tracking: TrackingStore, queue: Queue, apply_queue_url: str) -> int:
    count = 0
    for row in tracking.query_status(Status.CAPPED):
        queue.enqueue(apply_queue_url, {"pk": row["pk"]})
        count += 1
    return count


def handler(event, context):  # noqa: ANN001 - Lambda signature
    settings = get_settings()
    tracking = TrackingStore(settings.applications_table, region=settings.aws_region)
    queue = Queue(region=settings.aws_region)
    config_dir = Path(settings.config_dir)
    prefs = load_preferences(config_dir / "preferences.yaml")
    companies = load_watchlist(config_dir / "watchlist.yaml")

    total = 0
    with httpx.Client(headers={"User-Agent": "AppliedIn/0.1"}) as client:
        for raw in companies:
            try:
                company = resolve_company(raw, client)
            except Exception:  # resolution failure must not kill the whole poll
                log.exception("ATS resolution failed for %s", raw.name)
                continue
            if company.discovery is not DiscoveryMode.FEED:
                continue  # crawl companies are handled by the Fargate crawler
            try:
                total += discover_company(
                    company, prefs, tracking, queue, client, settings.tailor_queue_url
                )
            except Exception:  # one bad company must not kill the whole poll
                log.exception("discovery failed for %s", company.name)

    requeued = requeue_capped(tracking, queue, settings.apply_queue_url)
    log.info("discovery done: enqueued=%d requeued_capped=%d", total, requeued)
    return {"enqueued": total, "requeued_capped": requeued}
