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

from core.logging import get_logger
from core.models import JobRecord

log = get_logger(__name__)

# A big careers page takes real time: filters to set, "Load more" to click, and
# now a judgement per posting. The first Apple crawl needed just over seven
# minutes, so a seven-minute ceiling killed the next one at the finish line.
TIMEOUT_S = 1800  # 30 minutes
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


def _task(company: str, url: str, brief: str) -> str:
    return f"""Find the open jobs on this company's careers page that fit this owner.

COMPANY: {company}
CAREERS PAGE: {url}

{brief}

Open the page and actually look for postings. Many careers pages need work before
they show anything: a "See all jobs" or "View openings" link, a department filter,
a search box, or a "Load more" button that has to be clicked several times. Use
the site's own search and filters with the roles and topics above — that is far
faster than scrolling everything — and follow them until you are seeing the real
list. If the page turns out to be a wrapper around a job board (Greenhouse, Ashby,
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

Close the tabs you opened, then write your result as JSON to the file you are told
about and repeat it in your reply:

{{"jobs": [{{"title": "...", "url": "...", "location": "...", "summary": "...",
            "score": 0-10, "why": "..."}}],
  "board": "greenhouse|ashby|lever|workday|none — if this page is a wrapper",
  "note": "<anything the owner should know: no openings, a login wall, a CAPTCHA>"}}"""


async def find_jobs(company: str, careers_url: str, *, prefs: object = None,
                    model: str = "") -> tuple[list[JobRecord], str, str]:
    """Postings on `careers_url` that fit `prefs`. Returns (jobs, board, note)."""
    from tools.claude_chrome import available, run_task

    ok, why = available()
    if not ok:
        return [], "", why

    report, problem = await run_task(
        _task(company, careers_url, _brief(prefs)),
        report_key="jobs", model=model, timeout_s=TIMEOUT_S)
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
