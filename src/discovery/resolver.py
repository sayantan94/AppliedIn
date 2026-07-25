"""Resolve a company's career-page URL to an ATS + feed token.

The watchlist is seeded with career-page URLs, not hand-written ATS types.
Resolution is two-stage:

1. URL patterns — many career links ARE the ATS board
   (boards.greenhouse.io/<token>, jobs.lever.co/<handle>, ...).
2. Page scan — a custom careers page (company.com/careers) usually embeds or
   links its ATS; we fetch the HTML and look for the same signatures.

Anything unrecognized resolves to CRAWL, so the career-site crawler covers it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from core.logging import get_logger
from core.models import DiscoveryMode

log = get_logger(__name__)

# Fetch as a real browser. The old "AppliedIn/0.1" UA got 403/406/429'd by
# careers sites (OpenAI, Meta, Perplexity, Uber…), which ALSO blocked ATS
# detection (a blocked page can't be scanned for its embedded ATS), dumping the
# company into the slow crawl path. A normal Chrome UA + Accept headers fixes both.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class AtsMatch:
    ats: str
    board: str  # feed token/handle, or the full base URL for workday
    discovery: DiscoveryMode = DiscoveryMode.FEED


# --- URL-pattern detectors ---------------------------------------------------
# Each returns an AtsMatch or None. Order matters only for disjoint hosts.

def _greenhouse(host: str, path: str, url: str) -> AtsMatch | None:
    # boards.greenhouse.io/<token>, job-boards.greenhouse.io/<token>,
    # or an embed URL ...?for=<token>
    if "greenhouse.io" in host:
        m = re.search(r"[?&]for=([a-z0-9_-]+)", url)
        if m:
            return AtsMatch("greenhouse", m.group(1))
        parts = [p for p in path.split("/") if p]
        if parts:
            return AtsMatch("greenhouse", parts[0])
    return None


def _lever(host: str, path: str, url: str) -> AtsMatch | None:
    if "lever.co" in host:
        parts = [p for p in path.split("/") if p]
        if parts:
            return AtsMatch("lever", parts[0])
    return None


def _ashby(host: str, path: str, url: str) -> AtsMatch | None:
    if "ashbyhq.com" in host:
        parts = [p for p in path.split("/") if p]
        if parts:
            return AtsMatch("ashby", parts[0])
    return None


def _smartrecruiters(host: str, path: str, url: str) -> AtsMatch | None:
    if "smartrecruiters.com" in host:
        parts = [p for p in path.split("/") if p]
        if parts:
            return AtsMatch("smartrecruiters", parts[0])
    return None


def _workday(host: str, path: str, url: str) -> AtsMatch | None:
    # <tenant>.<dc>.myworkdayjobs.com/<lang>/<site> -> cxs JSON base
    if "myworkdayjobs.com" in host:
        tenant = host.split(".")[0]
        parts = [p for p in path.split("/") if p]
        site = parts[-1] if parts else ""
        if site:
            base = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
            return AtsMatch("workday", base)
    return None


_URL_DETECTORS = (_greenhouse, _lever, _ashby, _smartrecruiters, _workday)

# Signatures to look for inside a fetched custom careers page (many custom pages
# embed or link their real ATS board). Only ATSes we have a feed adapter for.
_PAGE_SIGNATURES = (
    (re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)"),
     "greenhouse"),
    (re.compile(r"api\.greenhouse\.io/v1/boards/([a-z0-9_-]+)"), "greenhouse"),
    (re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)"), "lever"),
    (re.compile(r"api\.lever\.co/v0/postings/([a-z0-9_-]+)"), "lever"),
    (re.compile(r"(?:jobs\.)?ashbyhq\.com/([a-z0-9_-]+)"), "ashby"),
    (re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([a-z0-9_-]+)"), "ashby"),
    (re.compile(r"api\.smartrecruiters\.com/v1/companies/([a-z0-9_-]+)"), "smartrecruiters"),
)


def detect_from_url(url: str) -> AtsMatch | None:
    parsed = urlparse(url)
    host, path = parsed.netloc.lower(), parsed.path
    for detector in _URL_DETECTORS:
        match = detector(host, path, url)
        if match:
            return match
    return None


def detect_from_page(html: str) -> AtsMatch | None:
    for pattern, ats in _PAGE_SIGNATURES:
        m = pattern.search(html)
        if m:
            return AtsMatch(ats, m.group(1))
    return None


# Board APIs we can query directly by slug, and how to tell a real board from a
# 200 that means nothing. Ordered by how common they are.
_BOARD_PROBES = (
    ("greenhouse", "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false",
     lambda d: bool(d.get("jobs"))),
    ("ashby", "https://api.ashbyhq.com/posting-api/job-board/{slug}",
     lambda d: bool(d.get("jobs"))),
    ("lever", "https://api.lever.co/v0/postings/{slug}?mode=json",
     lambda d: bool(d) if isinstance(d, list) else False),
)


def _registrable(host: str) -> str:
    """The company label of a hostname — the one before the public suffix.

    Careers sites are routinely served from a multi-label subdomain
    (<anything>.jobs.<company>.<tld>). Taking the FIRST label picks up whatever
    marketing word happens to lead the hostname, which then collides with an
    unrelated board that really is named that — and a stranger's postings enter
    the pipeline. Always reduce to the company label.
    """
    labels = [x for x in (host or "").lower().split(".") if x]
    if len(labels) < 2:
        return labels[0] if labels else ""
    # Handle two-part suffixes (.co.uk, .com.au) before falling back to labels[-2].
    if len(labels) >= 3 and labels[-2] in {"co", "com", "net", "org", "ac", "gov"}:
        return labels[-3]
    return labels[-2]


def _slug_candidates(name: str, careers_url: str) -> list[str]:
    """Plausible board slugs for a company: its name, and its careers domain.

    The name alone is not enough ("Google DeepMind" is never a slug) and the
    domain alone is not either (scale.com hosts the "scaleai" board), so try both.
    """
    out: list[str] = []
    n = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if n:
        out.append(n)
        hyphen = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
        if hyphen and hyphen != n:
            out.append(hyphen)
    domain = _registrable(urlparse(careers_url or "").netloc)
    if domain and domain not in out:
        out.append(domain)
    # "Scale AI" -> scaleai is covered by `n`; scale.com -> "scale" by the domain.
    return out


def _same_company(board_name: str, company: str, slug: str) -> bool:
    """Does this board actually belong to this company?

    A slug can collide with an unrelated board, and pulling a stranger's postings
    into the pipeline means tailoring and applying to the wrong company. Require
    the board's own name to line up with the company name (or the slug we asked
    for) before trusting it.
    """
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())  # noqa: E731
    b, c = norm(board_name), norm(company)
    if not b or not c:
        return False
    return b in c or c in b or b == norm(slug)


def probe_boards(name: str, careers_url: str,
                 client: httpx.Client | None = None) -> AtsMatch | None:
    """Ask the known board APIs whether this company has a board.

    A custom careers page that renders its jobs client-side leaves no ATS
    signature in the HTML, so page scanning finds nothing and we fall back to an
    expensive, flaky browser crawl. Usually the real board is one request away —
    Databricks looked like a crawl target while a Greenhouse board with 800 jobs
    answered on the first probe.
    """
    if client is None:
        return None
    for slug in _slug_candidates(name, careers_url):
        for ats, template, has_jobs in _BOARD_PROBES:
            try:
                resp = client.get(template.format(slug=slug), timeout=12,
                                  follow_redirects=True)
                if resp.status_code != 200:
                    continue
                payload = resp.json()
                if not has_jobs(payload):
                    continue
                # A live board is not necessarily THIS company's board.
                owner = _board_owner(ats, slug, payload, client)
                if owner and not _same_company(owner, name, slug):
                    log.info("probe: %s board %r belongs to %r, not %s — ignoring",
                             ats, slug, owner, name)
                    continue
                log.info("probed %s -> %s board %r", name, ats, slug)
                return AtsMatch(ats, slug)
            except (httpx.HTTPError, ValueError):
                continue  # unreachable or not JSON — just try the next probe
    return None


def _board_owner(ats: str, slug: str, payload: object,
                 client: httpx.Client) -> str:
    """The name the board reports for itself, when it publishes one."""
    if isinstance(payload, dict) and payload.get("name"):
        return str(payload["name"])
    if ats == "greenhouse":
        try:
            r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}",
                           timeout=10, follow_redirects=True)
            if r.status_code == 200:
                return str((r.json() or {}).get("name") or "")
        except (httpx.HTTPError, ValueError):
            return ""
    return ""


def resolve(careers_url: str, client: httpx.Client | None = None,
            name: str = "") -> AtsMatch:
    """Resolve a career-page URL to an AtsMatch; CRAWL if unrecognized."""
    direct = detect_from_url(careers_url)
    if direct:
        return direct

    # Custom page: fetch and scan for an embedded/linked ATS.
    if client is not None:
        try:
            resp = client.get(careers_url, timeout=20, follow_redirects=True)
            resp.raise_for_status()
            embedded = detect_from_page(resp.text)
            if embedded:
                return embedded
        except httpx.HTTPError as exc:
            log.warning("could not fetch %s for ATS detection: %s", careers_url, exc)

    # Nothing in the HTML — the page may render its jobs from an API. Ask the
    # board APIs directly before settling for a crawl.
    probed = probe_boards(name, careers_url, client)
    if probed:
        return probed

    return AtsMatch("custom", careers_url, discovery=DiscoveryMode.CRAWL)
