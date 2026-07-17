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

# Signatures to look for inside a fetched custom careers page.
_PAGE_SIGNATURES = (
    (re.compile(r"boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)"), "greenhouse"),
    (re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)"), "lever"),
    (re.compile(r"(?:jobs\.)?ashbyhq\.com/([a-z0-9_-]+)"), "ashby"),
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


def resolve(careers_url: str, client: httpx.Client | None = None) -> AtsMatch:
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

    return AtsMatch("custom", careers_url, discovery=DiscoveryMode.CRAWL)
