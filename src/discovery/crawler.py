"""Career-site crawler for companies with no usable ATS feed.

Two tiers, cheapest first:
  1. render + extract — headless render (Playwright) + one-shot LLM extract.
     Fast and cheap; good for static/simple pages.
  2. browser-use — a real browser agent that types the search, applies filters
     and pages/scrolls. Escalated to ONLY when tier 1 finds nothing, which is
     what happens on client-rendered search UIs (e.g. jobs.apple.com/…/search).

Then the same filter / dedup / enqueue path as feed discovery. Mode-agnostic:
stores come from the factory. The extractor is injectable so it's testable (an
injected extractor also disables the browser escalation — tests stay offline).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx

from core.logging import get_logger
from core.models import JobRecord

from .relevance import relevant
from .resolver import detect_from_page
from .watchlist import CompanyConfig, Preferences

log = get_logger(__name__)

_MAX_HTML = 200_000  # cap the page text we hand the model


def _run_async(coro: Any) -> Any:
    """Run an async coroutine from this sync crawler. If a loop is already
    running on this thread (the finder agent calls the crawler inside ADK's
    event loop), run it on a dedicated thread so we never nest ``asyncio.run``."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # no loop here — the common (cron/daemon) path

    import threading

    box: dict[str, Any] = {}
    def _worker() -> None:
        box["v"] = asyncio.run(coro)
    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    return box.get("v")


def _search_terms(prefs: Preferences) -> list[str]:
    """The queries the crawler types into the site's search box — the target
    TITLES plus the AI/agents include_keywords, so it surfaces what preferences
    actually ask for (not just a generic first title)."""
    seen: set[str] = set()
    terms: list[str] = []
    for t in [*prefs.titles, *prefs.include_keywords]:
        t = (t or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            terms.append(t)
    return terms[:5] or ["software engineer"]


_CRAWL_CEILING_S = 600  # 10 min per company — enterprise portals (Google, Oracle)
# otherwise grind a full-watchlist sweep for HOURS; a crawl that needs longer is
# stuck, not thorough. The company is simply skipped until the next sweep.


def _browser_extract(url: str, company: str, prefs: Preferences) -> list[JobRecord]:
    """Tier-2 escalation: drive the page with a browser agent (browser-use) and
    map the postings it uncovers to JobRecords."""
    import asyncio
    from urllib.parse import urljoin

    from core.config import get_settings
    from tools.browser_crawl import crawl

    async def _bounded():
        return await asyncio.wait_for(
            crawl(url, company, _search_terms(prefs), get_settings().browser_model),
            timeout=_CRAWL_CEILING_S)

    try:
        items = _run_async(_bounded())
    except TimeoutError:
        log.warning("%s: browser crawl hit the %ds ceiling — skipping until the next sweep",
                    company, _CRAWL_CEILING_S)
        return []
    return [
        JobRecord(company=company, job_id=str(it["job_id"]), title=it.get("title", ""),
                  # normalize relative URLs (e.g. /en-us/details/…) to absolute so the
                  # seen-list dedup and the clickable link are consistent
                  jd_url=urljoin(url, it.get("url", "")), jd_text=it.get("title", ""), ats="custom")
        for it in items if it.get("job_id")
    ]


def _render_page(url: str) -> str | None:
    """Render a JS-heavy career page with a headless browser and return its HTML.

    Custom portals (Apple, etc.) load listings with JavaScript, so a plain fetch
    sees an empty shell. Playwright renders it. Returns None if Playwright isn't
    installed or the render fails (caller falls back to a plain fetch)."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        log.warning("playwright not installed — can't render JS pages (run ./setup.sh, "
                    "or `uv sync --extra runtime && uv run playwright install chromium`)")
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=30_000)
            html = page.content()
            browser.close()
        log.info("rendered %s with browser (%d chars)", url, len(html))
        return html
    except Exception as exc:
        log.error("browser render failed for %s: %s", url, exc)
        return None


def _default_extractor(html: str, company: str) -> list[JobRecord]:
    """LLM extraction of postings from a careers page (mode-selected model)."""
    from litellm import completion

    from core.config import get_settings

    prompt = (
        "Extract every job posting from this careers page as a JSON array of "
        '{"job_id","title","url"} objects. job_id is a stable id from the URL if '
        "present, else a slug of the title. Return ONLY the JSON array.\n\n"
        f"{html[:_MAX_HTML]}"
    )
    try:
        resp = completion(model=get_settings().litellm_model,
                          messages=[{"role": "user", "content": prompt}])
        text = resp["choices"][0]["message"]["content"]
    except Exception as exc:
        log.error("%s: LLM extraction call failed: %s", company, exc)
        return []
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        log.warning("%s: model returned no JSON array (%r…)", company, text[:120])
        return []
    try:
        items = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        log.warning("%s: could not parse extracted jobs: %s", company, exc)
        return []
    return [
        JobRecord(company=company, job_id=str(it["job_id"]), title=it.get("title", ""),
                  jd_url=it.get("url", ""), jd_text=it.get("title", ""), ats="custom")
        for it in items if it.get("job_id")
    ]


def crawl_company(
    company: CompanyConfig,
    prefs: Preferences,
    stores: Any,
    *,
    client: httpx.Client | None = None,
    extractor: Callable[[str, str], list[JobRecord]] | None = None,
) -> int:
    """Crawl one custom career page; returns the number of jobs newly enqueued.

    Renders the page with a headless browser (JS career sites need it); falls
    back to a plain HTTP fetch if the browser isn't available."""
    html = _render_page(company.careers_url)
    if html is None:  # no browser / render failed -> plain fetch (static pages)
        own_client = client is None
        from .resolver import BROWSER_HEADERS
        client = client or httpx.Client(headers=BROWSER_HEADERS)
        try:
            resp = client.get(company.careers_url, timeout=20, follow_redirects=True)
            resp.raise_for_status()
            html = resp.text
        except httpx.HTTPError as exc:
            log.warning("crawl fetch failed for %s: %s", company.name, exc)
            return 0
        finally:
            if own_client:
                client.close()

    # If the page actually embeds a known ATS, we shouldn't be here — log it so
    # the watchlist can be corrected to feed mode.
    if embedded := detect_from_page(html):
        log.info("%s embeds %s — consider switching it to feed mode", company.name, embedded.ats)

    from core.events import emit

    extract = extractor or _default_extractor
    extracted = extract(html, company.name)
    from tools import seen

    jobs = relevant(extracted, prefs)
    # The cheap render under-renders JS search UIs (Apple returns a handful) — if
    # the agent judged nothing relevant, escalate to a real browser agent that
    # loads the full listing, then re-screen. Skipped when an extractor is
    # injected (tests stay offline) or the company is already exhausted.
    crawled = False
    if not jobs and extractor is None:
        if seen.crawl_exhausted(company.name):
            log.info("%s: browser crawl skipped — recent postings exhausted "
                     "(reset, or a new posting, will re-enable it)", company.name)
        else:
            log.info("%s: %d postings, 0 relevant from render+extract — escalating to "
                     "browser-use", company.name, len(extracted))
            extracted = _browser_extract(company.careers_url, company.name, prefs)
            jobs = relevant(extracted, prefs)
            crawled = True

    log.info("%s: extracted %d postings, %d relevant", company.name, len(extracted), len(jobs))
    if not extracted:
        log.warning("%s: extracted 0 postings — the page may need login, or the "
                    "browser agent couldn't find listings", company.name)
    elif not jobs:
        log.warning("%s: %d postings but the agent judged none relevant — sample: %s",
                    company.name, len(extracted), ", ".join(j.title for j in extracted[:3]))

    already = seen.load()
    jobs = [j for j in jobs if j.jd_url not in already]  # skip past-run URLs

    enqueued, new_jobs = 0, []
    for job in jobs:
        if stores.tracking.put_new(job):
            stores.queue.enqueue(stores.tailor_queue, {"pk": job.pk})
            emit("discovered", pk=job.pk, detail=f"{job.title} @ {job.company}", url=job.jd_url)
            new_jobs.append(job)
            enqueued += 1
    seen.mark(new_jobs)
    if crawled:  # 0 new -> mark exhausted so we stop re-crawling this company
        seen.record_crawl(company.name, enqueued)
    if enqueued:
        log.info("%s: enqueued %d new job(s)", company.name, enqueued)
    return enqueued
