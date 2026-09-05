"""Postings from a careers site's sitemap, for pages that refuse to be read.

Cloudflare turned away every plain request to jobs.fidelity.com — the page, the
search, the JSON the page itself calls — and the browser crawl came back with
nothing after six minutes. The sitemap answered at once: 528 postings, each with
its URL and the day it last changed. Nearly every careers site keeps one, because
search engines need it, and it sits outside the bot wall for the same reason.

The sitemap gives a URL and a date, not a title. The title is read off the URL's
own slug, which is enough for the relevance screen; the posting itself is read
later, in the browser if the page insists on one.
"""

from __future__ import annotations

import gzip
import hashlib
import re
from urllib.parse import parse_qsl, urlparse, urlunparse

import httpx

from core.logging import get_logger
from core.models import JobRecord

log = get_logger(__name__)

_DEFAULT_SITEMAPS = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
                     "/sitemap-jobs.xml", "/jobs-sitemap.xml", "/sitemap/jobs.xml")
_MAX_SITEMAPS = 25
_MAX_URLS = 20_000

_JOB_SEGMENTS = frozenset({
    "job", "jobs", "career", "careers", "position", "positions", "opening", "openings",
    "vacancy", "vacancies", "posting", "postings", "opportunity", "opportunities",
    "requisition", "requisitions", "req", "role", "roles", "offer", "offers",
})
_NOT_JOB = frozenset({
    "search", "search-results", "search-jobs", "results", "category", "categories",
    "job-categories", "location", "locations", "team", "teams", "department",
    "departments", "alert", "alerts", "faq", "faqs", "benefits", "students", "student",
    "early-career", "early-careers", "internships", "events", "blog", "news", "about",
    "why", "culture", "diversity", "process", "apply", "login", "register", "saved",
    "en", "en-us", "en-gb", "us", "uk", "de", "fr", "ie", "in",
})
# A country segment in the path is the only location the sitemap offers, and the
# screen needs it: without one, Dublin and Bangalore postings read as fits.
_COUNTRY_SEGMENTS = {
    "ie": "Ireland", "in": "India", "uk": "United Kingdom", "gb": "United Kingdom",
    "en-gb": "United Kingdom", "en-uk": "United Kingdom", "de": "Germany", "en-de": "Germany",
    "fr": "France", "en-fr": "France", "ca": "Canada", "en-ca": "Canada", "au": "Australia",
    "en-au": "Australia", "sg": "Singapore", "en-sg": "Singapore", "jp": "Japan",
    "en-jp": "Japan", "nl": "Netherlands", "es": "Spain", "it": "Italy", "pl": "Poland",
    "br": "Brazil", "mx": "Mexico", "cn": "China", "hk": "Hong Kong", "en-hk": "Hong Kong",
    "ch": "Switzerland", "se": "Sweden", "en-in": "India", "en-ie": "Ireland",
}
_ID_QUERY_KEYS = ("jobid", "job_id", "id", "reqid", "req_id", "requisitionid", "gh_jid",
                  "jid", "jobreqid", "postingid", "posting_id")
_ID_RX = re.compile(r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                    r"|[A-Za-z]{0,4}[-_]?\d{3,}[A-Za-z0-9_-]*|\d[A-Za-z0-9_-]{3,})$", re.I)
_ENTRY_RX = re.compile(r"<(url|sitemap)\b[^>]*>(.*?)</\1>", re.S | re.I)
_LOC_RX = re.compile(r"<loc\b[^>]*>\s*(.*?)\s*</loc>", re.S | re.I)
_LASTMOD_RX = re.compile(r"<lastmod\b[^>]*>\s*(.*?)\s*</lastmod>", re.S | re.I)
_SITEMAP_LINE_RX = re.compile(r"^\s*sitemap\s*:\s*(\S+)", re.I | re.M)
_WORD_RX = re.compile(r"[-_+.]+")


def _headers() -> dict:
    from .resolver import BROWSER_HEADERS

    return BROWSER_HEADERS


def _fetch(client: httpx.Client, url: str) -> bytes | None:
    try:
        r = client.get(url, timeout=20, follow_redirects=True)
    except httpx.HTTPError as exc:
        log.debug("sitemap fetch failed for %s: %s", url, exc)
        return None
    if r.status_code != 200:
        return None
    body = r.content
    if body[:2] == b"\x1f\x8b" or url.endswith(".gz"):
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError):
            return None
    return body


def _candidates(careers_url: str, client: httpx.Client) -> list[str]:
    p = urlparse(careers_url)
    root = urlunparse((p.scheme or "https", p.netloc, "", "", "", ""))
    seen: list[str] = []
    robots = _fetch(client, root + "/robots.txt")
    if robots:
        for m in _SITEMAP_LINE_RX.finditer(robots.decode("utf-8", "replace")):
            u = m.group(1).strip()
            if u not in seen:
                seen.append(u)
    for path in _DEFAULT_SITEMAPS:
        u = root + path
        if u not in seen:
            seen.append(u)
    return seen


def _unescape(s: str) -> str:
    return (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&#39;", "'"))


def _parse(xml: bytes) -> tuple[list[str], list[tuple[str, str]]]:
    text = xml.decode("utf-8", "replace")
    children: list[str] = []
    pages: list[tuple[str, str]] = []
    for kind, body in _ENTRY_RX.findall(text):
        loc = _LOC_RX.search(body)
        if not loc:
            continue
        url = _unescape(loc.group(1).strip())
        if kind.lower() == "sitemap":
            children.append(url)
        else:
            lm = _LASTMOD_RX.search(body)
            pages.append((url, lm.group(1).strip() if lm else ""))
    return children, pages


def _id_from(url: str) -> str:
    p = urlparse(url)
    for k, v in parse_qsl(p.query):
        if k.lower() in _ID_QUERY_KEYS and v.strip():
            return v.strip()
    parts = [s for s in p.path.split("/") if s]
    for s in parts:
        if s.lower() in _JOB_SEGMENTS:
            continue
        if _ID_RX.match(s):
            return s
    return ""


def is_job_url(url: str) -> bool:
    """Whether a sitemap entry looks like one posting rather than a page about them."""
    p = urlparse(url)
    parts = [s for s in p.path.split("/") if s]
    lowered = [s.lower() for s in parts]
    if any(s in _NOT_JOB and s not in ("en", "en-us", "en-gb", "us", "uk", "de", "fr", "ie", "in")
           for s in lowered):
        return False
    if _id_from(url) and any(s in _JOB_SEGMENTS for s in lowered):
        return True
    hits = [i for i, s in enumerate(lowered) if s in _JOB_SEGMENTS]
    if not hits:
        return False
    tail = parts[hits[0] + 1:]
    if not tail:
        return False
    slug = tail[-1]
    return len(_WORD_RX.split(slug)) >= 3 and not slug.isdigit()


def _title_from(url: str) -> str:
    p = urlparse(url)
    parts = [s for s in p.path.split("/") if s]
    words_of = lambda s: [w for w in _WORD_RX.split(s) if w and not w.isdigit()]  # noqa: E731
    best = ""
    for s in parts:
        if s.lower() in _JOB_SEGMENTS or s.lower() in _NOT_JOB or _ID_RX.match(s):
            continue
        if len(words_of(s)) > len(words_of(best)):
            best = s
    words = words_of(best)
    return " ".join(w if w.isupper() and len(w) <= 4 else w.capitalize() for w in words)[:120]


def job_from_url(company: str, url: str, lastmod: str = "") -> JobRecord | None:
    if not is_job_url(url):
        return None
    title = _title_from(url)
    if not title:
        return None
    job_id = _id_from(url) or hashlib.sha1(url.encode()).hexdigest()[:12]
    return JobRecord(company=company, job_id=job_id, title=title, jd_url=url,
                     jd_text="", ats="sitemap", posted_at=lastmod,
                     location=_location_from(url))


def _location_from(url: str) -> str:
    parts = [s.lower() for s in urlparse(url).path.split("/") if s]
    for s in parts[:2]:
        if s in _COUNTRY_SEGMENTS:
            return _COUNTRY_SEGMENTS[s]
    return ""


def sitemap_jobs(careers_url: str, company: str,
                 client: httpx.Client | None = None) -> list[JobRecord]:
    """Every posting the site's sitemap lists, or [] when it keeps none."""
    own = client is None
    client = client or httpx.Client(headers=_headers())
    try:
        queue = _candidates(careers_url, client)
        visited: set[str] = set()
        pages: list[tuple[str, str]] = []
        found_any = False
        while queue and len(visited) < _MAX_SITEMAPS and len(pages) < _MAX_URLS:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            body = _fetch(client, url)
            if not body:
                continue
            children, entries = _parse(body)
            if not children and not entries:
                continue
            found_any = True
            pages.extend(entries)
            children.sort(key=lambda u: ("job" not in u.lower(), u))
            queue = children + queue
            if not children and url in _defaults_for(careers_url):
                break
        if not found_any:
            return []
    finally:
        if own:
            client.close()
    jobs: dict[str, JobRecord] = {}
    for url, lastmod in pages:
        job = job_from_url(company, url, lastmod)
        if job and job.jd_url not in jobs:
            jobs[job.jd_url] = job
    log.info("%s: sitemap lists %d URL(s), %d of them postings", company, len(pages), len(jobs))
    return list(jobs.values())


def _defaults_for(careers_url: str) -> set[str]:
    p = urlparse(careers_url)
    root = urlunparse((p.scheme or "https", p.netloc, "", "", "", ""))
    return {root + path for path in _DEFAULT_SITEMAPS}
