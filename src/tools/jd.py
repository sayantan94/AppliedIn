"""Read a posting: its title, and the text the tailor works from."""

from __future__ import annotations

import re

import httpx

from core.logging import get_logger

log = get_logger(__name__)

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


def fetch_jd_meta(url: str) -> dict:
    """{'title', 'text'} for a posting, or empty strings when it cannot be read."""
    if not url:
        return {"title": "", "text": ""}
    if (hit := _from_ats(url)) and len(hit.get("text", "")) > 200:
        return hit
    from discovery.resolver import BROWSER_HEADERS

    try:
        with httpx.Client(headers=BROWSER_HEADERS, follow_redirects=True,
                          timeout=25) as client:
            html = client.get(url).text
    except httpx.HTTPError as exc:
        log.error("JD fetch failed for %s: %s", url, exc)
        return {"title": "", "text": ""}

    raw_title = (m.group(1).strip() if (m := _TITLE_RX.search(html)) else "")
    h1 = _text(m.group(1)) if (m := _H1_RX.search(html)) else ""
    title = h1 or _SPLIT_RX.split(_text(raw_title))[0].strip()
    text = _text(html)
    if len(text) < 400:
        # A page that builds itself in the browser gives a plain fetch almost
        # nothing, and a tailor working from nothing writes a worse résumé than
        # one that was never tailored. Read it the way a person would.
        if (seen := _from_chrome(url)) and len(seen.get("text", "")) > len(text):
            return seen
    return {"title": title[:90], "text": text}


def _from_chrome(url: str) -> dict | None:
    """Read the posting in the owner's own browser. Last resort, and slow."""
    import asyncio

    from tools.claude_chrome import available, run_task

    ok, _ = available()
    if not ok:
        return None
    task = (f"Open {url}, wait for it to load, and read the job posting.\n\n"
            "Close the tab, then write this JSON to the file you are told about "
            "and repeat it in your reply:\n"
            '{"title": "<the role title>", "description": "<the full posting text, '
            'verbatim: responsibilities, requirements, everything>"}')
    try:
        report, problem = asyncio.run(
            run_task(task, report_key="description", timeout_s=300))
    except RuntimeError:  # already inside a loop
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            report, problem = pool.submit(
                lambda: asyncio.run(run_task(task, report_key="description",
                                             timeout_s=300))).result()
    if problem or not report:
        log.warning("could not read %s in the browser: %s", url, problem)
        return None
    return {"title": str(report.get("title", ""))[:90],
            "text": str(report.get("description", ""))}


def fetch_jd(url: str) -> str:
    """Just the text."""
    return fetch_jd_meta(url).get("text", "")
