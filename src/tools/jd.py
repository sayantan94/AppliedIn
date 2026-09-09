"""Read a posting: its title, and the text the tailor works from."""

from __future__ import annotations

import re
import time

import httpx

from core.logging import get_logger

log = get_logger(__name__)


class PostingReadUnavailable(RuntimeError):
    """The reader failed before it could establish what is on the page."""


# A shared CLI outage must not launch another session for every row in a sweep.
# HTTP/ATS reads still run; only the browser reader backs off for five minutes.
_browser_retry: tuple[float, str] = (0, "")


def _check_browser_reader() -> None:
    if _browser_retry[0] > time.monotonic():
        raise PostingReadUnavailable(_browser_retry[1])


def _note_browser_problem(problem: str) -> None:
    from tools.claude_chrome import is_infrastructure

    global _browser_retry
    if is_infrastructure(problem):
        _browser_retry = (time.monotonic() + 300, problem)

_TAG_RX = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.S | re.I)
_TITLE_RX = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_H1_RX = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_SPLIT_RX = re.compile(r"\s[|\-–—]\s|\s+at\s+")


def _text(html: str) -> str:
    """Visible text, near enough for a language model to read."""
    html = _TAG_RX.sub(" ", html)
    html = re.sub(r"<br\s*/?>|</(p|div|li|h[1-6]|tr)>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
                .replace("&quot;", '"'))
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _from_ats(url: str) -> dict | None:
    """The posting from its board's own API, when the URL belongs to one.

    The big boards render in the browser and serve an empty shell to a plain
    fetch — reading jobs.ashbyhq.com over HTTP returns 39 characters and the word
    "About Us". They all publish the same posting as JSON, which is complete,
    fast and free, so ask for that instead of rendering a page to scrape it back.
    """
    from urllib.parse import urlparse

    import httpx

    u = urlparse(url)
    host, seg = (u.hostname or "").lower(), [p for p in u.path.split("/") if p]
    if len(seg) < 2:
        return None
    org, job_id = seg[0], seg[-1]
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as c:
            if "ashbyhq.com" in host:
                r = c.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}")
                r.raise_for_status()
                for j in r.json().get("jobs", []):
                    if job_id in str(j.get("jobUrl", "")) or job_id == str(j.get("id", "")):
                        return {"title": str(j.get("title", ""))[:90],
                                "text": (j.get("descriptionPlain")
                                         or _text(j.get("descriptionHtml", "")))}
            elif "greenhouse.io" in host:
                r = c.get(f"https://boards-api.greenhouse.io/v1/boards/{org}/jobs/{job_id}",
                          params={"content": "true"})
                r.raise_for_status()
                j = r.json()
                return {"title": str(j.get("title", ""))[:90],
                        "text": _text(j.get("content", ""))}
            elif "lever.co" in host:
                r = c.get(f"https://api.lever.co/v0/postings/{org}/{job_id}")
                r.raise_for_status()
                j = r.json()
                return {"title": str(j.get("text", ""))[:90],
                        "text": _text(j.get("descriptionPlain") or j.get("description", ""))}
    except Exception as exc:  # noqa: BLE001 — fall through to a plain fetch
        log.debug("board API miss for %s: %s", url, exc)
    return None


def fetch_jd_meta(url: str, kind: str = "jd") -> dict:
    """{'title', 'text'} for a posting, or empty strings when it cannot be read."""
    if not url:
        return {"title": "", "text": ""}
    if (hit := _from_ats(url)) and len(hit.get("text", "")) > 200:
        return hit
    from discovery.resolver import BROWSER_HEADERS

    # The disguise first, then bare. metacareers.com answers 400 to a request
    # that carries browser-like headers and 200 to one that carries none — the
    # imitation is what trips it — so a refusal is retried once, undisguised,
    # before this gives up.
    html = _get(url, BROWSER_HEADERS)
    if html is None:
        html = _get(url, {})
    if html is None:
        # Refused both ways. A bot wall in front of the page is not a missing
        # page: the sitemap lists the job, and a person would open it. Read it
        # the way they would before calling it unreadable.
        return _from_chrome(url, kind) or {"title": "", "text": ""}

    raw_title = (m.group(1).strip() if (m := _TITLE_RX.search(html)) else "")
    h1 = _text(m.group(1)) if (m := _H1_RX.search(html)) else ""
    title = h1 or _SPLIT_RX.split(_text(raw_title))[0].strip()
    text = _text(html)
    if len(text) < 400:
        # A page that builds itself in the browser gives a plain fetch almost
        # nothing, and a tailor working from nothing writes a worse résumé than
        # one that was never tailored. Read it the way a person would.
        if (seen := _from_chrome(url, kind)) and len(seen.get("text", "")) > len(text):
            return seen
    return {"title": title[:90], "text": text}


def _get(url: str, headers: dict) -> str | None:
    """The page, or None when the server refused or the request failed.

    A non-2xx response used to be returned as if it were the posting. Meta's
    400 page — "Sorry, something went wrong" — came back as 126 characters of
    job description, nothing downstream objected, and the tailor would have
    written a résumé against it. A refusal is not a page.
    """
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=25) as client:
            r = client.get(url)
    except httpx.HTTPError as exc:
        log.error("JD fetch failed for %s: %s", url, exc)
        return None
    if not 200 <= r.status_code < 300:
        log.warning("JD fetch refused for %s: HTTP %s", url, r.status_code)
        return None
    return r.text


def read_postings(urls: list[str], *, batch: int = 6, kind: str = "jd_sweep",
                  with_gone: bool = False):
    """Read several browser-only postings per session. {url: description}.

    Meta's postings cannot be read over HTTP at all, so each needs a real browser.
    Read one per session that cost ~5 minutes × 55 rows, serialised, each session
    waiting on any application in flight. The cost is session start-up, not page
    reads, so one session reads a handful.

    Two rules keep a batch honest. An entry is accepted only if its URL is one we
    asked for: a session that attributes one posting's text to another's URL is
    worse than one that fails, and it is the failure mode a batch invites. And a
    batch that comes back malformed loses only itself — the sweep carries on.
    """
    from tools.claude_chrome import TAB_HYGIENE

    if not urls:
        return ({}, set()) if with_gone else {}
    _check_browser_reader()
    ok, why = available()
    if not ok:
        raise PostingReadUnavailable(why)

    out: dict[str, str] = {}
    gone: set[str] = set()
    for i in range(0, len(urls), batch):
        chunk = urls[i:i + batch]
        listing = "\n".join(f"- {u}" for u in chunk)
        task = (f"Read these {len(chunk)} job postings, one at a time. For each: open the "
                f"URL, wait for it to load, read the whole posting — responsibilities, "
                f"requirements, everything — then close its tab.\n\n{listing}\n\n"
                f"{TAB_HYGIENE}\n"
                "Write this JSON to the file you are told about and repeat it in your "
                "reply, with one entry per URL EXACTLY as given above. If a posting has "
                "been removed — the page says the job is no longer available, or "
                "redirects to a not-available page — set \"gone\": true for it and "
                "leave its description empty:\n"
                '{"postings": [{"url": "<the url, verbatim>", "title": "<role title>", '
                '"gone": false, "description": "<the full posting text, verbatim>"}]}')
        try:
            report, problem = _run(run_task(task, report_key="postings",
                                            timeout_s=120 * len(chunk), kind=kind))
        except Exception as exc:  # noqa: BLE001 — one bad batch must not stop the sweep
            log.warning("posting batch failed: %s", exc)
            continue
        if problem or not isinstance(report.get("postings"), list):
            log.warning("posting batch returned nothing usable: %s", problem or "no list")
            if problem:
                _note_browser_problem(problem)
                if _browser_retry[0] > time.monotonic():
                    break  # Keep earlier results, and defer unread rows below.
            continue
        wanted = set(chunk)
        for entry in report["postings"]:
            if not isinstance(entry, dict):
                continue
            u = str(entry.get("url") or "").strip()
            text = str(entry.get("description") or "").strip()
            if u not in wanted:
                log.warning("posting batch returned a URL that was not asked for: %s", u[:80])
                continue
            if entry.get("gone") is True or _GONE_RX.search(text[:400]):
                gone.add(u)
                continue
            if len(text) < 400:
                continue
            out[u] = text
    return (out, gone) if with_gone else out


_GONE_RX = re.compile(r"no longer available|position[- ]not[- ]available|has been removed|"
                      r"job (?:posting )?(?:is )?closed", re.I)


def run_task(*args, **kwargs):
    from tools.claude_chrome import run_task as _rt

    return _rt(*args, **kwargs)


def available():
    from tools.claude_chrome import available as _av

    return _av()


def _run(coro):
    import asyncio

    try:
        return asyncio.run(coro)
    except RuntimeError:  # already inside a loop
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(coro)).result()


def _from_chrome(url: str, kind: str = "jd") -> dict | None:
    """Read the posting in the owner's own browser. Last resort, and slow."""
    import asyncio

    from tools.claude_chrome import available, run_task

    _check_browser_reader()
    ok, why = available()
    if not ok:
        raise PostingReadUnavailable(why)
    from tools.claude_chrome import TAB_HYGIENE

    task = (f"Open {url}, wait for it to load, and read the job posting.\n\n"
            f"{TAB_HYGIENE}\n"
            "Close the tab, then write this JSON to the file you are told about "
            "and repeat it in your reply:\n"
            '{"title": "<the role title>", "description": "<the full posting text, '
            'verbatim: responsibilities, requirements, everything>"}')
    try:
        report, problem = asyncio.run(
            run_task(task, report_key="description", timeout_s=300, kind=kind))
    except RuntimeError:  # already inside a loop
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            report, problem = pool.submit(
                lambda: asyncio.run(run_task(task, report_key="description",
                                             timeout_s=300, kind=kind))).result()
    if problem or not report:
        log.warning("could not read %s in the browser: %s", url, problem)
        why = problem or "The browser reader returned no posting report. Try reading it again."
        _note_browser_problem(why)
        raise PostingReadUnavailable(why)
    return {"title": str(report.get("title", ""))[:90],
            "text": str(report.get("description", ""))}


def fetch_jd(url: str, kind: str = "jd") -> str:
    """Just the text. `kind` says whether a browser read may wait for a live
    application ("jd_sweep") or is part of one ("jd")."""
    return fetch_jd_meta(url, kind=kind).get("text", "")
