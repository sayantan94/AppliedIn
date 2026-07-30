"""Career-site crawler for companies with no usable ATS feed.

Two tiers, cheapest first:
  1. fetch + extract — plain HTTP GET + one-shot LLM extract. Fast and cheap;
     good for static/simple pages.
  2. Chrome — the owner's real browser, driven by a `claude --chrome` subprocess
     that types the search, applies filters and pages/scrolls. Used when tier 1
     finds nothing, and ALWAYS for a company marked `discovery: browser`, whose
     careers page renders its listing client-side (jobs.apple.com/…/search and
     friends). Those pages give the plain fetch a short window of the listing;
     because that window usually contains a match or two, a "did we find
     anything" test reads it as success and the rest of the board is never seen.

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
from core.models import DiscoveryMode, JobRecord

from .relevance import relevant
from .resolver import detect_from_page
from .watchlist import CompanyConfig, Preferences

log = get_logger(__name__)

_MAX_HTML = 200_000  # cap the page text we hand the model



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


def _age_limit(company_name: str) -> float:
    """Hours of freshness to apply, 0 meaning no limit.

    The run's own window wins. Asking for the last 24 hours at four companies is a
    question about this scan, not a change to what those companies always want, so
    it must not fall through to or overwrite their saved preference.
    """
    from core import flags as _flags

    from .freshness import run_window

    if (w := run_window()) > 0:
        return w
    try:
        over = _flags.company_pref(company_name) or {}
        raw = over.get("max_age_hours")
        if raw in (None, ""):
            raw = _flags.get_flag("max_age_hours", "0")
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def _enqueue(company: CompanyConfig, jobs: list, stores: Any) -> int:
    """Drop anything too old, dedup against past runs, queue what is genuinely new.

    The age check lives HERE because every path arrives here: the feed adapters, the
    page extractor and the browser crawl. Putting it in any one of them would have
    left the others unfiltered, and the browser path is the one most likely to
    surface an ancient listing.
    """
    from core import flags as _flags
    from core.events import emit
    from tools import seen

    from .freshness import describe, is_fresh

    limit = _age_limit(company.name)
    if limit:
        before = len(jobs)
        jobs = [j for j in jobs if is_fresh(getattr(j, "posted_at", ""), limit)]
        if before != len(jobs):
            log.info("%s: %d of %d posting(s) were newer than %gh",
                     company.name, len(jobs), before, limit)
        undated = sum(1 for j in jobs if not getattr(j, "posted_at", ""))
        if undated:
            # Say it, because it is the difference between "nothing new was posted"
            # and "this board does not publish dates so the filter did nothing".
            log.info("%s: %d kept posting(s) carry no publish date, so the age "
                     "limit could not judge them", company.name, undated)

    already = seen.load()
    jobs = [j for j in jobs if j.jd_url not in already]

    enqueued, new_jobs = 0, []
    for job in jobs:
        if stores.tracking.put_new(job):
            stores.queue.enqueue(stores.tailor_queue, {"pk": job.pk})
            emit("discovered", pk=job.pk, detail=(
                f"{job.title} @ {job.company}"
                + (f" (posted {d})" if (d := describe(getattr(job, "posted_at", ""))) else "")),
                url=job.jd_url)
            new_jobs.append(job)
            enqueued += 1
    seen.mark(new_jobs)
    if enqueued:
        log.info("%s: enqueued %d new job(s)", company.name, enqueued)
    return enqueued


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
    from core import flags as _flags

    # This company's own preferences, before anything reads them. The feed path
    # did this and the crawl path did not, so a company with overrides was screened
    # on the global rules here and on its own rules there — and the companies most
    # likely to have overrides are exactly the big career sites that land in this
    # path. The overrides also steer the browser crawl's search terms, so applying
    # them late would have been too late: the wrong queries were already typed.
    prefs = _flags.effective_prefs(company.name, prefs)
    if _flags.company_pref(company.name):
        log.info("%s: using company preference overrides %s",
                 company.name, sorted(_flags.company_pref(company.name)))

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
            # A site that REFUSES the plain fetch is the clearest case for the
            # browser, not a reason to give up. metacareers.com answers 400 and
            # then 429 to a scripted request and serves the same page normally to
            # a real session. Returning 0 here meant a company could never be
            # crawled at all, while the run reported "no new roles matched your
            # preferences" — which was untrue, and unfixable by changing them.
            # INFO, not a warning: for a site marked browser this is the expected
            # path, and a warning every run trains the reader to ignore warnings.
            log.info("%s: plain fetch refused (%s) — reading the page in the "
                     "browser instead", company.name, str(exc)[:90])
            html = ""
        finally:
            if own_client:
                client.close()

    if not html and extractor is None:
        found = _browser_extract(company.careers_url, company.name, prefs)
        jobs = relevant(found, prefs)
        # Three outcomes that a single "0 new" line used to blur together, and
        # they need different actions from the owner: fix the URL, loosen the
        # preferences, or nothing at all.
        if not found:
            log.warning("%s: read 0 postings from the careers page, by fetch or by "
                        "browser. This is a READING problem, not a preferences one "
                        "— check the careers_url in watchlist.yaml", company.name)
        elif not jobs:
            log.info("%s: browser read %d posting(s), none matched the preferences "
                     "— sample: %s", company.name, len(found),
                     ", ".join(j.title for j in found[:3]))
        else:
            log.info("%s: browser read %d posting(s), %d relevant",
                     company.name, len(found), len(jobs))
        return _enqueue(company, jobs, stores)

    # If the page actually embeds a known ATS, we shouldn't be here — log it so
    # the watchlist can be corrected to feed mode.
    if embedded := detect_from_page(html):
        log.info("%s embeds %s — consider switching it to feed mode", company.name, embedded.ats)

    from core.events import emit

    extract = extractor or _default_extractor
    extracted = extract(html, company.name)
    from tools import seen

    jobs = relevant(extracted, prefs)
    # Escalate to a real browser agent that loads the FULL listing, then re-screen.
    # Two ways in. Skipped when an extractor is injected (tests stay offline).
    #
    #   discovery: browser — the page is a client-rendered search UI and the plain
    #     fetch is known to see only a fraction of it. Crucially the old rule
    #     ("escalate when nothing was relevant") never fired for these: a short
    #     window containing one match looks exactly like a complete page with one
    #     match, so a rescan kept returning the same handful and new roles further
    #     down the listing were never fetched at all.
    #   nothing relevant — the page may simply not have rendered; worth one look.
    force_browser = company.discovery is DiscoveryMode.BROWSER
    if extractor is None and (force_browser or not jobs):
        log.info("%s: %s — reading the full listing in the browser", company.name,
                 "client-rendered careers page" if force_browser
                 else "nothing from the plain fetch")
        seen_by_browser = _browser_extract(company.careers_url, company.name, prefs)
        # Union, not replacement: the plain fetch occasionally catches a posting
        # the browser agent scrolls past, and losing one to gain thirty is still
        # a loss for the job it dropped.
        by_url = {j.jd_url: j for j in extracted}
        by_url.update({j.jd_url: j for j in seen_by_browser})
        extracted = list(by_url.values())
        jobs = relevant(extracted, prefs)

    log.info("%s: extracted %d postings, %d relevant", company.name, len(extracted), len(jobs))
    if not extracted:
        log.warning("%s: extracted 0 postings — the page may need login, or the "
                    "browser agent couldn't find listings", company.name)
    elif not jobs:
        log.warning("%s: %d postings but the agent judged none relevant — sample: %s",
                    company.name, len(extracted), ", ".join(j.title for j in extracted[:3]))

    # The per-company title filter narrows on top, exactly as the feed path does.
    # The crawl ignored it entirely, so "only Staff titles at Rivian" held for feed
    # companies and quietly did nothing for career-site ones.
    kws = _flags.company_filter(company.name)
    if kws:
        before = len(jobs)
        jobs = [j for j in jobs if _flags.title_matches_filter(j.title, kws)]
        log.info("%s: title filter %s kept %d/%d", company.name, kws, len(jobs), before)

    return _enqueue(company, jobs, stores)
