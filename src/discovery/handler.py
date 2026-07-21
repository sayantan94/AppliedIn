"""Discovery — the fast, dumb finder (mode-agnostic).

Per run: for each feed company, resolve its ATS from the careers URL, fetch ->
stage-1 filter -> per-company watermark + first-run backfill cap -> conditional
put (dedup) -> enqueue survivors to the pipeline queue. Crawl-mode companies go
to the crawler. Runs identically local (Redis) or cloud (DynamoDB/SQS): all
storage comes from the mode factory, never constructed directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from core.config import get_settings
from core.logging import get_logger
from core.models import DiscoveryMode, Status
from core.stores import make_stores

from .adapters import ADAPTERS
from .relevance import relevant
from .resolver import BROWSER_HEADERS, resolve
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
    tracking: Any,
    queue: Any,
    client: httpx.Client,
    tailor_queue_url: str,
) -> int:
    """Discover one company; returns the number of jobs newly enqueued."""
    from core.events import emit

    adapter = ADAPTERS.get(company.ats)
    if adapter is None:
        log.warning("%s: no adapter for ats=%r — check the careers_url", company.name, company.ats)
        return 0

    try:
        fetched = adapter.fetch(company, client)
    except Exception as exc:
        log.error("%s: feed fetch failed (%s) — check the board token in watchlist.yaml",
                  company.name, exc)
        return 0

    matched = relevant(fetched, prefs)
    log.info("%s: fetched %d postings, %d relevant", company.name, len(fetched), len(matched))
    if fetched and not matched:
        sample = ", ".join(j.title for j in fetched[:3])
        log.warning("%s: %d postings but the agent judged none relevant — sample: %s",
                    company.name, len(fetched), sample)

    from tools import seen

    already = seen.load()
    matched = [j for j in matched if j.jd_url not in already]  # skip past-run URLs

    wm_row = tracking.get(_watermark_pk(company.name))
    if wm_row is None:  # first run — cap the backfill
        matched = matched[:BACKFILL_CAP]

    enqueued, new_jobs = 0, []
    for job in matched:
        if tracking.put_new(job):  # False => already seen, skip
            queue.enqueue(tailor_queue_url, {"pk": job.pk})
            emit("discovered", pk=job.pk, detail=f"{job.title} @ {job.company}", url=job.jd_url)
            new_jobs.append(job)
            enqueued += 1
    seen.mark(new_jobs)

    if enqueued:
        log.info("%s: enqueued %d new job(s) for the pipeline", company.name, enqueued)
    tracking.set_status(_watermark_pk(company.name), Status.FOUND, last_poll="done")
    return enqueued


def list_watchlist_companies() -> list[str]:
    """The company names in the watchlist, in file order — so the UI can offer a
    'run discovery for just these' picker instead of always sweeping all."""
    config_dir = Path(get_settings().config_dir)
    return [c.name for c in load_watchlist(config_dir / "watchlist.yaml")]


def run_discovery(only: list[str] | None = None) -> dict:
    """Find new jobs across the watchlist and enqueue them for the pipeline.

    `only` scopes the run to specific companies (case-insensitive names, matched
    against the watchlist); None/empty = the whole watchlist. The UI passes the
    user's picked companies so discovery isn't always all-or-nothing.

    Mode-agnostic: stores come from the factory, so this is the SAME finder on
    a Mac (Redis) or on AWS (DynamoDB/SQS). Callable from a CLI (local) or a
    Lambda (cloud).
    """
    settings = get_settings()
    stores = make_stores(settings)
    config_dir = Path(settings.config_dir)
    prefs = load_preferences(config_dir / "preferences.yaml")
    companies = load_watchlist(config_dir / "watchlist.yaml")
    if only:
        wanted = {n.strip().lower() for n in only if n and n.strip()}
        companies = [c for c in companies if c.name.strip().lower() in wanted]
        log.info("discovery scoped to %d/%s companies: %s",
                 len(companies), "all", ", ".join(c.name for c in companies) or "(none matched)")

    total = crawl_total = 0
    crawl_companies: list[CompanyConfig] = []
    with httpx.Client(headers=BROWSER_HEADERS) as client:
        for raw in companies:
            try:
                company = resolve_company(raw, client)
            except Exception:
                log.exception("ATS resolution failed for %s", raw.name)
                continue
            if company.discovery is not DiscoveryMode.FEED:
                crawl_companies.append(company)  # custom career page -> crawler
                continue
            try:
                total += discover_company(
                    company, prefs, stores.tracking, stores.queue, client, stores.tailor_queue
                )
            except Exception:
                log.exception("discovery failed for %s", company.name)

    # Crawl-mode companies (no usable feed) -> the browser crawler.
    if crawl_companies:
        from .crawler import crawl_company

        for company in crawl_companies:
            try:
                crawl_total += crawl_company(company, prefs, stores)
            except Exception:
                log.exception("crawl failed for %s", company.name)

    log.info("discovery done: feed=%d crawl=%d", total, crawl_total)
    return {"enqueued": total, "crawled": crawl_total}


def handler(event, context):  # noqa: ANN001 - Lambda signature (cloud cron)
    return run_discovery()
