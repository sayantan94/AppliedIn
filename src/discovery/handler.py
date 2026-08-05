"""Discovery — the fast, dumb finder (mode-agnostic).

Per run: for each feed company, resolve its ATS from the careers URL, fetch ->
stage-1 filter -> per-company watermark + first-run backfill cap -> conditional
put (dedup) -> enqueue survivors to the pipeline queue. Crawl-mode companies go
to the crawler. Runs identically local (Redis) or cloud (DynamoDB/SQS): all
storage comes from the mode factory, never constructed directly.
"""

from __future__ import annotations

import time as _time

import threading
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
    match = resolve(company.careers_url, client, name=company.name)
    # An explicit `discovery: browser` in the watchlist SURVIVES resolution.
    # The resolver infers ats and board from the careers URL, and it can only ever
    # guess "feed" or "crawl" — BROWSER is a judgement the resolver cannot make,
    # because a client-rendered page looks like an ordinary one until you fetch it
    # and count what came back. Overwriting it here silently undid the setting:
    # Netflix was marked browser, resolved back to crawl, and kept extracting ten
    # postings out of hundreds. Apple only escaped because it had `ats: custom`
    # set, which skips resolution entirely.
    discovery = (company.discovery if company.discovery is DiscoveryMode.BROWSER
                 else match.discovery)
    log.info(
        "resolved %s (%s) -> ats=%s discovery=%s",
        company.name, company.careers_url, match.ats, discovery.value,
    )
    return company.model_copy(
        update={"ats": match.ats, "board": match.board, "discovery": discovery}
    )


def discover_company(
    company: CompanyConfig,
    prefs: Preferences,
    tracking: Any,
    queue: Any,
    client: httpx.Client,
    tailor_queue_url: str,
    profile_id: str = "",
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

    from core import flags as _flags

    # Per-company preference OVERRIDES first (dashboard): the same fields as the
    # global preferences, but for one company — "Google, but Staff+ only, score
    # bar 6". Anything left unset inherits the global value, so a company with no
    # overrides screens exactly as before.
    eff = _flags.effective_prefs(company.name, prefs)
    if eff is not prefs:
        log.info("%s: using company preference overrides %s",
                 company.name, sorted(_flags.company_pref(company.name)))

    # AGE FIRST, before the relevance screen. This path has its own enqueue and
    # never reaches the crawler's filter, so the window silently did not apply to
    # feed companies at all — which are exactly the ones carrying exact publish
    # dates. Filtering here also means a stale posting never costs a relevance
    # call: Scale AI fetched 208 and screened all of them to find 40.
    from .crawler import _age_limit
    from .freshness import age_hours, describe, is_fresh

    limit = _age_limit(company.name)
    if limit:
        kept, stale = [], []
        for j in fetched:
            (kept if is_fresh(getattr(j, "posted_at", ""), limit) else stale).append(j)
        if stale:
            for j in stale[:20]:      # a board can be mostly old; log a readable slice
                age = age_hours(getattr(j, "posted_at", ""))
                log.info("%s: SKIPPED %s — published %s (%s), outside the %gh window",
                         company.name, j.title[:60],
                         (getattr(j, "posted_at", "") or "?")[:10],
                         f"{age:.0f}h ago" if age is not None else "unreadable date", limit)
            from . import passed_over

            passed_over.record(
                getattr(tracking, "r", None), company.name, stale, "too_old",
                f"published outside the {limit:g} hour window this scan asked for")
            log.info("%s: %d of %d posting(s) were published in the last %gh",
                     company.name, len(kept), len(fetched), limit)
        fetched = kept

    # Same for the feed path: a board with no dates yields roles that can never
    # appear under Fresh, and silence there reads as a scan that found nothing.
    if fetched and all(not getattr(j, "posted_at", "") for j in fetched):
        from core.events import emit as _emit

        _emit("running", agent="finder", company=company.name,
              pk=f"meta#undated#{company.name.lower()}",
              detail=(f"{company.name} publishes no posting dates, so its roles "
                      f"cannot show under Fresh. They are on the Pipeline board "
                      f"under Found."))

    before_screen = list(fetched)
    matched = relevant(fetched, eff)
    if (screened_out := [j for j in before_screen
                         if j.jd_url not in {m.jd_url for m in matched}]):
        from . import passed_over

        passed_over.record(getattr(tracking, "r", None), company.name, screened_out,
                           "not_relevant",
                           "did not match your title, seniority or keyword preferences")
    # Then the title filter, which NARROWS on top rather than replacing.
    kws = _flags.company_filter(company.name)
    if kws:
        before = len(matched)
        matched = [j for j in matched if _flags.title_matches_filter(j.title, kws)]
        log.info("%s: title filter %s kept %d/%d", company.name, kws, len(matched), before)
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
    # Take only the best few per run. Tailoring is the expensive stage, and a big
    # board can match dozens of roles that are merely acceptable; the relevance
    # screen already returns them best-first.
    top_n = getattr(eff, "max_new_per_run", 0) or 0
    if top_n > 0 and len(matched) > top_n:
        log.info("%s: keeping the top %d of %d matches this run",
                 company.name, top_n, len(matched))
        matched = matched[:top_n]

    # A company's own identity wins over the run's. Picking "personal 2" for this
    # discovery run is a one-off; setting it on the company is a standing rule,
    # and the standing rule is the more specific statement of intent.
    co_profile = _flags.company_pref(company.name).get("profile_id") or ""
    stamp_profile = co_profile or profile_id

    enqueued, new_jobs = 0, []
    for job in matched:
        if tracking.put_new(job):  # False => already seen, skip
            if stamp_profile:
                # Stamped at discovery so the whole batch applies from one
                # identity — the résumé is rendered to match when it's tailored.
                tracking.set_status(job.pk, Status.FOUND, profile_id=stamp_profile)
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


def company_for_url(url: str) -> str:
    """If a job URL belongs to a watchlist company's ATS (same host + org slug),
    return that company's proper name (so a single-role tailor of a Rivian Ashby
    URL is labeled 'Rivian', not 'Rivianvw.Tech'). Else ''."""
    from urllib.parse import urlparse
    u = urlparse(url)
    host, seg = (u.hostname or "").lower(), [p for p in u.path.split("/") if p]
    org = (seg[0].lower() if seg else "")
    for c in load_watchlist(Path(get_settings().config_dir) / "watchlist.yaml"):
        cu = urlparse(c.careers_url or "")
        ch, cseg = (cu.hostname or "").lower(), [p for p in cu.path.split("/") if p]
        corg = (cseg[0].lower() if cseg else "")
        if host and host == ch and (not corg or corg == org):
            return c.name
    return ""


def add_watchlist_company(name: str, careers_url: str = "") -> dict:
    """Append a company to watchlist.yaml (the dashboard's 'Add company').

    Appends a properly-quoted YAML entry (mode: gated — new portals always start
    gated) and round-trips the file through the loader afterwards; if the write
    somehow broke parsing, the original file is restored and an error returned.
    ats/board stay unset — the finder auto-resolves them on the first discover,
    same as any hand-added entry."""
    import yaml as _yaml

    name = (name or "").strip()
    careers_url = (careers_url or "").strip()
    if not name:
        return {"ok": False, "error": "Company name is required."}
    if careers_url and not careers_url.lower().startswith(("http://", "https://")):
        careers_url = "https://" + careers_url
    path = Path(get_settings().config_dir) / "watchlist.yaml"
    existing = load_watchlist(path)
    if any(c.name.strip().lower() == name.lower() for c in existing):
        return {"ok": False, "error": f"{name} is already on the watchlist."}

    entry: dict = {"name": name}
    if careers_url:
        entry["careers_url"] = careers_url
    entry["mode"] = "gated"
    block = _yaml.safe_dump([entry], default_flow_style=False,
                            sort_keys=False, allow_unicode=True)
    original = path.read_text()
    with path.open("a") as f:
        f.write("\n" + "".join(f"  {ln}\n" for ln in block.splitlines()))
    try:
        load_watchlist(path)  # round-trip: the file must still parse
    except Exception as exc:
        path.write_text(original)
        log.error("add_watchlist_company: entry broke the yaml (%s) — restored", exc)
        return {"ok": False, "error": "Could not add the company (bad characters?)."}
    log.info("watchlist: added %s (%s)", name, careers_url or "no careers URL yet")
    return {"ok": True, "companies": list_watchlist_companies()}


# One discovery at a time, whoever asked for it. The scheduled sweep and the
# dashboard's Discover button both land here, and they were able to run at once:
# a single-company run the owner had just started would be interleaved with a
# full watchlist sweep, so the log they were reading described neither.
# Which companies are being scanned RIGHT NOW, and the lock guarding that set.
#
# This used to be one global lock, so a scan of any company refused a scan of every
# other one. Two crawls of DIFFERENT employers share nothing — separate sites,
# separate feeds, separate browser sessions, and their findings land under
# different keys — so the only thing worth preventing is two scans of the SAME
# company, which would duplicate the work and race each other's writes.
_LOCK = threading.Lock()
_SCANNING: set[str] = set()

# The ONE company being read right now, and how far through the batch we are.
#
# Claims are taken for the whole batch up front, so `_SCANNING` says "these 47 are
# spoken for", not "these 47 are being crawled". Reporting the claim set as the
# scanning set meant a 47 company sweep showed all 47 as in progress with an
# identical clock, and there was no way to tell a run that was working from one
# that had hung. Crawls are sequential: exactly one company is ever being read.
_ACTIVE: dict[str, object] = {"company": "", "since": 0.0, "done": 0, "total": 0}


def active() -> dict:
    """Which company is being read right now, and progress through the batch."""
    import time as _t

    with _LOCK:
        a = dict(_ACTIVE)
    a["elapsed_s"] = int(_t.time() - float(a["since"] or 0)) if a["since"] else 0
    return a


def _set_active(company: str, done: int, total: int) -> None:
    import time as _t

    with _LOCK:
        _ACTIVE.update({"company": company, "since": _t.time(),
                        "done": done, "total": total})


def _clear_active() -> None:
    with _LOCK:
        _ACTIVE.update({"company": "", "since": 0.0, "done": 0, "total": 0})


def scanning() -> set[str]:
    """The companies being scanned right now, lowercased."""
    with _LOCK:
        return set(_SCANNING)


def _claim(names: set[str]) -> set[str]:
    """Take the ones nobody else is scanning. Atomic, so two requests arriving
    together cannot both claim the same company."""
    with _LOCK:
        free = names - _SCANNING
        _SCANNING.update(free)
        return free


def _release(names: set[str]) -> None:
    with _LOCK:
        _SCANNING.difference_update(names)


def run_discovery(only: list[str] | None = None, profile_id: str = "",
                  max_age_hours: float | int | None = None,
                  manual: bool = False) -> dict:
    """Find new jobs across the watchlist and enqueue them for the pipeline.

    `only` scopes the run to specific companies (case-insensitive names, matched
    against the watchlist); None/empty = the whole watchlist. The UI passes the
    user's picked companies so discovery isn't always all-or-nothing.

    `profile_id` stamps every job this run finds with the identity it should be
    sent under, so a whole batch can go out from one address without touching each
    job afterwards. Empty = the default profile decides at apply time.

    Mode-agnostic: stores come from the factory, so this is the SAME finder on
    a Mac (Redis) or on AWS (DynamoDB/SQS). Callable from a CLI (local) or a
    Lambda (cloud).
    """
    # Scope this run to recently published roles, when asked. Run scoped: it is a
    # question about this scan, so it neither reads nor writes the companies' own
    # settings.
    from .freshness import set_run_window

    set_run_window(max_age_hours)

    targets = _targets(only)
    if not targets:
        return {"enqueued": 0, "crawled": 0, "skipped": "no companies matched"}
    claimed = _claim(targets)
    if not claimed:
        busy = ", ".join(sorted(targets))
        log.info("already scanning %s — skipping this request", busy)
        return {"enqueued": 0, "crawled": 0,
                "skipped": f"already scanning {busy}"}
    if len(claimed) < len(targets):
        # Partial overlap: get on with the rest rather than refusing everything.
        log.info("already scanning %s — running the other %d",
                 ", ".join(sorted(targets - claimed)), len(claimed))
    # Captured before the first company, so only a Stop pressed after this point
    # can end the run. A board that was already paused does not.
    from core import flags as _flags

    epoch0 = _flags.stop_epoch()
    try:
        return _run_discovery(sorted(claimed), profile_id, manual, epoch0)
    finally:
        _release(claimed)


def _targets(only: list[str] | None) -> set[str]:
    """The companies a request would scan, lowercased, before anything is claimed.

    Resolved here rather than inside the run so the claim can be per company: to
    know a request does not collide with a running one, you have to know which
    companies it covers.
    """
    settings = get_settings()
    names = {c.name.strip().lower()
             for c in load_watchlist(Path(settings.config_dir) / "watchlist.yaml")}
    if only:
        wanted = {n.strip().lower() for n in only if n and n.strip()}
        return names & wanted
    # An un-scoped run honours the skip toggles; an explicit pick overrides them.
    from core import flags as _flags

    return names - set(_flags.skipped_companies() or ())


def _stop_requested(manual: bool = False, epoch0: int = 0) -> bool:
    """True when this run should stop, checked between companies.

    A sweep is an hour of sequential browser work. Reading the flag only before
    the cycle starts meant Pause could not reach a run already going, and Stop
    killed the live session only for the loop to pick up the next company. This
    is the check that lets either one land inside a sweep.

    A scheduled sweep stops while paused: it has no owner behind it, and pause
    means "do not go and find work". A MANUAL run is the owner speaking now, so
    the pause flag must not veto it — pressing Discover on a paused board used
    to return ok and scan nothing, six times in one evening. It stops only for a
    Stop asserted after it began, which the epoch comparison detects.
    """
    try:
        from core import flags as _flags

        if manual:
            return _flags.stop_epoch() != epoch0
        return bool(_flags.paused())
    except Exception:  # noqa: BLE001 - a stop check must never break a scan
        return False


def _run_discovery(only: list[str] | None, profile_id: str,
                   manual: bool = False, epoch0: int = 0) -> dict:
    # Progress through the batch, so the board can say "12 of 47" rather than
    # implying all 47 are being read at once.
    done_n = 0
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
    else:
        # Un-scoped run: honor the owner's skip toggles. An explicit pick
        # (`only`) overrides a skip — checking a skipped company means it.
        from core import flags as _flags
        skipped = _flags.skipped_companies()
        if skipped:
            before = len(companies)
            companies = [c for c in companies
                         if c.name.strip().lower() not in skipped]
            if before != len(companies):
                log.info("discovery: excluding %d skipped company(ies)",
                         before - len(companies))

    total = crawl_total = 0
    batch_n = len(companies)      # what "12 of 47" counts against
    from . import scan_log

    _r = getattr(stores.tracking, "r", None)
    scan_log.start_run(_r, batch_n)
    crawl_companies: list[CompanyConfig] = []
    with httpx.Client(headers=BROWSER_HEADERS) as client:
        for raw in companies:
            if _stop_requested(manual, epoch0):
                log.info("discovery stopped by the owner after %d company(ies)", done_n)
                break
            try:
                company = resolve_company(raw, client)
            except Exception:
                log.exception("ATS resolution failed for %s", raw.name)
                continue
            if company.discovery is not DiscoveryMode.FEED:
                crawl_companies.append(company)  # custom career page -> crawler
                continue
            try:
                _set_active(company.name, done_n, batch_n)
                done_n += 1
                _t0 = _time.time()
                _n = discover_company(company, prefs, stores.tracking,
                                      stores.queue, client,
                                      stores.tailor_queue, profile_id)
                total += _n
                scan_log.finished(_r, company.name, found=_n, relevant=_n,
                                  enqueued=_n, seconds=_time.time() - _t0)
            except Exception:
                log.exception("discovery failed for %s", company.name)

    # Crawl-mode companies (no usable feed) -> the browser crawler.
    if crawl_companies:
        from .crawler import crawl_company

        for company in crawl_companies:
            if _stop_requested(manual, epoch0):
                log.info("discovery stopped by the owner after %d company(ies)", done_n)
                break
            try:
                _set_active(company.name, done_n, batch_n)
                done_n += 1
                _t0 = _time.time()
                _n = crawl_company(company, prefs, stores)
                crawl_total += _n
                scan_log.finished(_r, company.name, found=_n, relevant=_n,
                                  enqueued=_n, seconds=_time.time() - _t0)
            except Exception:
                log.exception("crawl failed for %s", company.name)

    _clear_active()      # nothing is being read once the batch is finished
    log.info("discovery done: feed=%d crawl=%d", total, crawl_total)
    return {"enqueued": total, "crawled": crawl_total}


def handler(event, context):  # noqa: ANN001 - Lambda signature (cloud cron)
    return run_discovery()
