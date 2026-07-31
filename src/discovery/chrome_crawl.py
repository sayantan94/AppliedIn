"""Find postings on a careers page by reading it in the owner's own Chrome.

Feed-backed boards (Greenhouse, Ashby, Lever) are fetched over HTTP and need none
of this. This is for the rest: the custom careers pages that render only for a
real session, hide their listings behind a search box, or paginate with a button.
A headless browser gets an empty shell from those, so the crawler saw nothing and
the company looked like it had no openings.

Returns the same JobRecord list as the feed adapters, so everything downstream —
relevance, dedup, the queue — cannot tell where a posting came from.
"""

from __future__ import annotations

import asyncio
import time

from core.logging import get_logger
from core.models import JobRecord

log = get_logger(__name__)

# A big careers page takes real time: filters to set, "Load more" to click, and
# now a judgement per posting. The first Apple crawl needed just over seven
# minutes, so a seven-minute ceiling killed the next one at the finish line.
TIMEOUT_S = 1800  # 30 minutes
# Often enough that a watching owner sees movement, rare enough that a long
# crawl does not fill the activity feed with its own waiting.
HEARTBEAT_S = 45
# A cap, not a target. Apple's board returned 274 matches for one search in one
# city, so a low cap silently turns "what is open" into "the first few" — and the
# owner cannot tell the difference from the outside. The session says when it
# truncated, so at least the ceiling is visible.
MAX_JOBS = 60


def _as_score(v: object) -> int | None:
    """A 0-10 score, or None when the session did not give a usable one."""
    try:
        n = int(float(str(v)))
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 10 else None


def _brief(prefs: object) -> str:
    """The owner's criteria, as the session should read them.

    Everything the preferences file says, not the two fields that happened to be
    threaded through: a crawl that knows the titles but not the exclusions
    happily returns product managers.
    """
    def _list(name: str) -> list[str]:
        return [str(x).strip() for x in (getattr(prefs, name, None) or []) if str(x).strip()]

    parts = []
    if t := _list("titles"):
        parts.append("ROLES — the job must be one of these, or an obvious variant "
                     f"(SDE, SWE, Backend/Platform/ML/AI Engineer all count):\n  {', '.join(t)}")
    if sen := _list("seniority"):
        parts.append(f"SENIORITY — at or above: {', '.join(sen)}. Junior, associate and "
                     "new-grad postings are not a fit however well the topic matches.")
    if k := _list("include_keywords"):
        parts.append("TOPICS THAT RAISE FIT — not required, but a role touching these "
                     f"scores higher:\n  {', '.join(k)}")
    if x := _list("exclude_keywords"):
        parts.append(f"NEVER A FIT — skip anything matching:\n  {', '.join(x)}")
    if loc := _list("locations"):
        parts.append(f"LOCATIONS — {', '.join(loc)}. Remote counts. Include a role whose "
                     "location you cannot determine rather than dropping it: a wrong guess "
                     "is easy to discard, a missing role is invisible.")
    if notes := str(getattr(prefs, "notes", "") or "").strip():
        parts.append(f"HARD CONSTRAINTS FROM THE OWNER — these override everything "
                     f"above:\n  {notes[:600]}")
    return "\n\n".join(parts)


def _queries(prefs: object) -> str:
    """The searches to actually run, as COMBINATIONS rather than single words.

    One query per title returns that title and nothing else, so a role doing
    exactly the wanted work under a plain name is missed. One query per topic
    returns roles that never mention the title, which is how the interesting ones
    are usually named. The PAIR is what finds the overlap, and no single query
    covers all three cases — so the pairs are listed explicitly instead of left to
    the session to invent, because "search the site" reliably becomes one search.
    """
    def _list(name: str) -> list[str]:
        return [str(x).strip() for x in (getattr(prefs, name, None) or []) if str(x).strip()]

    titles, topics = _list("titles")[:3], _list("include_keywords")[:5]
    if not titles and not topics:
        return ""
    pairs = [f"{t} {k}" for t in titles[:2] for k in topics[:3]]
    queries: list[str] = []
    for q in [*pairs, *topics, *titles]:          # pairs first: they are the point
        if q.lower() not in {x.lower() for x in queries}:
            queries.append(q)
    lines = "\n".join(f"  {q}" for q in queries[:12])
    return ("\nSEARCHES TO RUN — work through these and MERGE what they return. Do not\n"
            "stop after the first one: each is deliberately a different slice, and the\n"
            "pairs at the top are the ones that find a plainly titled role doing the\n"
            "right work. If the site lets filters stack (a keyword facet that turns\n"
            "each term into a chip), apply several at once instead of searching one\n"
            "term at a time.\n"
            f"{lines}\n")


def _window_block(hours: float) -> str:
    """The instruction that makes a bounded scan cheap as well as correct.

    Two separate things are being asked for. Correctness: report only roles the
    employer published inside the window. Cost: nearly every board sorts newest
    first, so once a listing is older than the window everything below it is too,
    and the scan can stop instead of reading the whole board. That turns a sweep
    from the cost of all postings into the cost of new ones, which is what makes
    scanning forty companies feasible at all.

    It is deliberately explicit that an undated listing is REPORTED rather than
    dropped: the alternative is a model silently discarding roles because a page
    happened not to print a date, which looks identical to a company having posted
    nothing.
    """
    if hours <= 0:
        return ""
    label = (f"{int(hours)} hours" if hours < 48
             else f"{int(hours / 24)} days" if hours % 24 == 0
             else f"{hours:g} hours")
    return f"""
ONLY ROLES POSTED IN THE LAST {label}
This scan is looking for what is NEW, so a role published outside that window is
not wanted however well it fits.

  1. Find how the board shows age: a date, "Posted 3 days ago", "New". Sort by
     newest first if the page offers it.
  2. Read down the list and STOP once you reach roles older than {label}. Boards
     almost always order newest first, so everything below the first stale one is
     stale too. Do not page through the rest to be thorough: reading the whole
     board is the cost this instruction exists to avoid.
  3. Report the age of every role you do return, in "posted", exactly as the page
     phrased it.
  4. If a listing shows NO age, report it with "posted" empty. It is kept and
     judged elsewhere. Never drop a role for lacking a date, and never guess one
     from its position in the list.
  5. If the board offers no age anywhere, say so in the note and return what fits.
     That is a fact about the board, not an empty result.
"""


def _task(company: str, url: str, brief: str, site_rules: str = "",
          max_age_hours: float = 0.0) -> str:
    return f"""Find the open jobs on this company's careers page that fit this owner.

COMPANY: {company}
CAREERS PAGE: {url}

{brief}
{_window_block(max_age_hours)}
{site_rules}

Open the page and actually look for postings. Many careers pages need work before
they show anything: a "See all jobs" or "View openings" link, a department filter,
a search box, or a "Load more" button that has to be clicked several times. Use
the site's own search and filters with the roles and topics above — that is far
faster than scrolling everything — and follow them until you are seeing the real
list.

SORT BY NEWEST FIRST if the site offers it (a "Sort" control, "Most recent", or a
date column). This listing gets cut off at a fixed number of postings, so the
order decides WHICH ones survive the cut: newest-first means the cut drops roles
already seen on earlier runs, while a relevance or default sort means new postings
further down are never returned at all.

Search with COMBINATIONS, not just single terms. One query per role title returns
that title only; pairing a title with a topic ("Senior Software Engineer" plus
"agentic") surfaces roles whose title is generic but whose work is exactly the
fit, and a topic on its own ("ai agents") catches the ones titled nothing like the
target. Run several queries and merge what they return rather than trusting one. If the page turns out to be a wrapper around a job board (Greenhouse, Ashby,
Lever, Workday), say so: it can then be read directly, which is faster and
complete.

For each posting collect the exact title, the direct link, and the location as
shown. Also give a short `summary` of what the role is and what it requires, and
a `score` from 0 to 10 for how well it fits THIS owner with one line of `why`.
You have just read the listing, so judging it here saves reading it again. Be
honest and use the range: a 7 that should be a 3 wastes their time, and scoring
everything 8 tells them nothing. Do NOT open each posting to write the summary —
if the list does not say enough, leave it empty and it will be read properly
later.

Do not invent a posting, and do not include one whose link you have not actually
seen on the page. An imagined job wastes a real application.

Stop at {MAX_JOBS} postings. If there were clearly more, say so in the note and
include how you would narrow it — a URL with search or location parameters is
worth more than a truncated list.

WHEN EACH ROLE WAS POSTED, in the "posted" field
Boards say this in different ways: an explicit date, "Posted 3 days ago", "New",
or a date only on the posting itself. Report what the LISTING says, verbatim and
untranslated: "2026-07-27", "3 days ago", "yesterday", "New" are all fine. Leave
it empty rather than guessing, and never infer a date from where a role sits in
the list. An empty field is read as unknown and the role is kept; a wrong date
gets a fresh role discarded or an old one treated as new.

Close the tabs you opened, then write your result as JSON to the file you are told
about and repeat it in your reply:

{{"jobs": [{{"title": "...", "url": "...", "location": "...", "summary": "...",
            "score": 0-10, "why": "...", "posted": "..."}}],
  "board": "greenhouse|ashby|lever|workday|none — if this page is a wrapper",
  "note": "<anything the owner should know: no openings, a login wall, a CAPTCHA>"}}"""


def _posted_iso(raw: object) -> str:
    """An ISO date from whatever a careers page said about age.

    Career pages phrase this every way there is: "2026-07-27", "Posted 3 days ago",
    "yesterday", "New", "27 Jul 2026". Demanding one format from a model reading a
    page would mean throwing away most of what it correctly saw, so this accepts
    the common shapes and returns empty for anything it cannot place. Empty means
    unknown, and an unknown date keeps the posting.
    """
    import re
    from datetime import datetime, timedelta, timezone

    text = str(raw or "").strip().lower()
    if not text:
        return ""
    now = datetime.now(timezone.utc)
    if text in ("new", "just posted", "today", "just now"):
        return now.isoformat()
    if text in ("yesterday",):
        return (now - timedelta(days=1)).isoformat()
    if (m := re.search(r"(\d+)\s*(hour|hr|h|day|d|week|w|month|mo)", text)):
        n = int(m.group(1))
        unit = m.group(2)
        hours = (n if unit in ("hour", "hr", "h")
                 else n * 24 if unit in ("day", "d")
                 else n * 24 * 7 if unit in ("week", "w")
                 else n * 24 * 30)
        return (now - timedelta(hours=hours)).isoformat()
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%b %d, %Y", "%d/%m/%Y", "%m/%d/%Y",
                "%B %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(str(raw).strip(), fmt).replace(
                tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return ""


async def find_jobs(company: str, careers_url: str, *, prefs: object = None,
                    model: str = "") -> tuple[list[JobRecord], str, str]:
    """Postings on `careers_url` that fit `prefs`. Returns (jobs, board, note)."""
    from tools.claude_chrome import available, run_task

    ok, why = available()
    if not ok:
        return [], "", why

    # Per-site discovery rules, if we have learned any for this careers page.
    # Never raises: a bad note must not stop a crawl.
    try:
        from tools.company_skills import instructions_for
        site_rules = instructions_for(careers_url, company)
    except Exception:  # noqa: BLE001
        log.debug("could not load company skills for %s", company, exc_info=True)
        site_rules = ""

    # Say so if this is about to wait. A scan that sits silent for ten minutes
    # because an application is being filled looks identical to one that hung.
    from core.events import emit
    from tools.claude_chrome import applies_running

    if (n := applies_running()):
        emit("running", agent="finder", company=company,
             detail=f"{company}: waiting for {n} application(s) to finish before scanning")

    from .freshness import run_window

    # A browser crawl of a big careers site takes minutes: Apple's took twenty one,
    # and in that time the run emitted nothing at all. Silence for twenty minutes
    # is indistinguishable from a hang, so the owner is left watching a spinner
    # wondering whether to restart something that is working perfectly well.
    #
    # So it reports that it is still going, with the elapsed time, which is the one
    # fact that separates slow from stuck. Cancelled in a finally, because a
    # heartbeat that outlives its run would be a worse lie than no heartbeat.
    started = time.monotonic()

    async def _heartbeat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            mins = int((time.monotonic() - started) // 60)
            emit("running", agent="finder", company=company,
                 detail=(f"{company}: still reading the careers page"
                         + (f", {mins} min so far" if mins else "")))
            log.info("%s: crawl still running after %d min", company, mins)

    beat = asyncio.create_task(_heartbeat())
    try:
        report, problem = await run_task(
            _task(company, careers_url, _brief(prefs) + _queries(prefs), site_rules,
                  max_age_hours=run_window()),
            report_key="jobs", model=model, timeout_s=TIMEOUT_S, kind="crawl")
    finally:
        beat.cancel()
    if problem:
        log.warning("chrome crawl of %s failed: %s", company, problem)
        return [], "", problem

    jobs: list[JobRecord] = []
    for row in (report.get("jobs") or [])[:MAX_JOBS]:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        url = str(row.get("url") or "").strip()
        # A posting we cannot link to is one the owner cannot apply to, and a
        # title with no URL is usually the model summarising rather than reading.
        if not title or not url.startswith("http"):
            continue
        jobs.append(JobRecord(
            company=company,
            job_id=url.rstrip("/").rsplit("/", 1)[-1][:80] or title[:80],
            title=title,
            jd_url=url,
            # What the listing showed. Enough for the relevance screen, so a job
            # that will be discarded never costs a page load; the full posting is
            # read later for the ones that survive.
            jd_text=str(row.get("summary") or "").strip(),
            crawl_score=_as_score(row.get("score")),
            crawl_why=str(row.get("why") or "").strip()[:200],
            location=str(row.get("location") or "").strip(),
            # Whatever the listing said, normalised. Relative phrases ("3 days
            # ago") are as common as dates on career pages, so the parser takes
            # both rather than demanding ISO from a model reading a web page.
            posted_at=_posted_iso(row.get("posted")),
            ats="crawl",
        ))

    board = str(report.get("board") or "").strip().lower()
    board = board if board in ("greenhouse", "ashby", "lever", "workday") else ""
    note = str(report.get("note") or "").strip()
    log.info("chrome crawl %s: %d posting(s)%s", company, len(jobs),
             f" — page is a {board} wrapper" if board else "")
    return jobs, board, note


def find_jobs_sync(company: str, careers_url: str, **kw: object) -> tuple[list, str, str]:
    """Blocking wrapper — discovery runs in a plain worker thread."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(find_jobs(company, careers_url, **kw))  # type: ignore[arg-type]
    # Already inside a loop (the daemon): run it on its own.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(
            lambda: asyncio.run(find_jobs(company, careers_url, **kw))).result()  # type: ignore[arg-type]
