"""Career-site crawler — for watchlist companies with no usable feed.

Loads the careers page in a real browser, extracts postings into normalized
:class:`JobRecord`s (LLM-assisted; the extractor is injected and faked in
tests), then runs the identical dedup/enqueue path as feed discovery:
conditional ``put_new`` (dedup by pk) -> tailor queue. The stage-1 preference
filter lives in the discovery package, so the predicate is injected here as
``matches`` (wired from preferences.yaml by the container entrypoint).

Minimal async Page surface: ``page.goto(url)``, ``page.content() -> str``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.config import Settings
from core.logging import get_logger
from core.models import JobRecord
from core.storage.queue import Queue
from core.storage.tracking import TrackingStore

log = get_logger(__name__)

# (careers-page html, company_cfg) -> normalized job records
Extractor = Callable[[str, dict], list[JobRecord]]

_MAX_HTML_CHARS = 120_000
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)

_SYSTEM_PROMPT = (
    "You extract job postings from a careers-page HTML. Reply with ONLY a JSON "
    'array; each element is {"job_id": str, "title": str, "jd_url": str, '
    '"location": str, "jd_text": str}. Use a stable unique id per posting '
    "(from the URL if possible). No prose."
)


@dataclass
class CrawlDeps:
    """Injected collaborators for one crawl run."""

    tracking: TrackingStore
    queue: Queue
    settings: Settings
    extractor: Extractor
    matches: Callable[[JobRecord], bool]  # stage-1 predicate (injected)
    page: Any = None  # injected in tests; None -> real Playwright launch


async def run_crawl(company_cfg: dict, *, deps: CrawlDeps) -> int:
    """Crawl one company's careers page; returns the number newly enqueued.

    ``company_cfg`` needs at least ``name`` and ``careers_url`` (and ``ats``
    for records, default "custom" — crawled portals apply via the agentic
    engine unless a scripted adapter exists).
    """
    page = deps.page
    if page is None:  # pragma: no cover - real browser only in the container
        from .browser import launch_page

        page = await launch_page()

    await page.goto(company_cfg["careers_url"])
    html = await page.content()

    jobs = deps.extractor(html, company_cfg)
    enqueued = 0
    for job in jobs:
        if not deps.matches(job):
            continue
        if deps.tracking.put_new(job):  # False => already seen (dedup)
            deps.queue.enqueue(deps.settings.tailor_queue_url, {"pk": job.pk})
            enqueued += 1

    log.info("crawl %s: extracted=%d enqueued=%d", company_cfg["name"], len(jobs), enqueued)
    return enqueued


def llm_extract(html: str, company_cfg: dict) -> list[JobRecord]:
    """Default LLM-assisted extractor (Strands, lazy import). Tests inject a
    fake extractor instead; this runs only in the container."""
    from core.llm.provider import get_model
    from strands import Agent

    agent = Agent(model=get_model(), system_prompt=_SYSTEM_PROMPT)
    reply = str(agent(html[:_MAX_HTML_CHARS]))
    return parse_postings(reply, company_cfg)


def parse_postings(text: str, company_cfg: dict) -> list[JobRecord]:
    """Defensively parse the model's reply; malformed rows are skipped."""
    match = _JSON_ARRAY_RE.search(text)
    if match is None:
        log.warning("crawler extraction returned no JSON array")
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        log.warning("crawler extraction returned invalid JSON")
        return []

    records: list[JobRecord] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            records.append(
                JobRecord(
                    company=company_cfg["name"],
                    job_id=str(item["job_id"]),
                    title=str(item["title"]),
                    jd_url=str(item.get("jd_url", "")),
                    jd_text=str(item.get("jd_text", "")),
                    location=str(item.get("location", "")),
                    ats=str(company_cfg.get("ats", "custom")),
                )
            )
        except KeyError:
            log.warning("skipping malformed posting row: %r", item)
    return records
