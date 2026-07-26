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
    """Read the page in the owner's own Chrome and map what it finds to records.

    This is the tier that matters for custom careers pages. A headless render
    gets an empty shell from anything that builds itself for a real session, or
    hides its listings behind a search box or a "Load more" button — so the
    company looked like it had no openings when it had thirty.
    """
    from urllib.parse import urljoin

    from core.config import get_settings

    from .chrome_crawl import find_jobs_sync

    jobs, board, note = find_jobs_sync(
        company, url, prefs=prefs,
        model=(getattr(get_settings(), "chrome_model", "") or ""))
    if board:
        # Worth saying loudly: a wrapper around a real board should be read from
        # that board's feed, which is faster, free and complete.
        log.info("%s: careers page is a %s wrapper — switch it to feed mode in "
                 "watchlist.yaml", company, board)
    if note:
        log.info("%s: %s", company, note[:200])
    # Relative links normalised so dedup and the clickable URL agree.
    for j in jobs:
        j.jd_url = urljoin(url, j.jd_url)
    return jobs


def _render_page(url: str) -> str | None:
    """Deliberately no browser here.

    The cheap tier is a plain fetch: it handles server-rendered career pages, and
    for the ones it cannot handle a headless render did not help either — those
    pages build themselves for a real session, which is what the Chrome
    escalation below is for.
    """
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
    if not jobs and extractor is None:
        log.info("%s: nothing from the plain fetch — reading the page in the browser",
                 company.name)
        extracted = _browser_extract(company.careers_url, company.name, prefs)
        jobs = relevant(extracted, prefs)

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
    if enqueued:
        log.info("%s: enqueued %d new job(s)", company.name, enqueued)
    return enqueued
