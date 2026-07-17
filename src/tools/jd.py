"""Fetch the full job-description text from a posting URL.

Discovery only captures a title + URL; the scorer and tailor need the real JD.
This renders the posting with a headless browser (JS-heavy career pages need it)
and returns the visible text. Sync (Playwright sync API) — callers inside an
event loop should run it via ``asyncio.to_thread``.
"""

from __future__ import annotations

from core.logging import get_logger

log = get_logger(__name__)

_MAX = 20_000  # cap the JD text we keep


def fetch_jd(url: str) -> str:
    """Return the posting's visible text, or '' if it can't be fetched."""
    if not url:
        return ""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        log.warning("playwright not installed — can't fetch JD text (run ./setup.sh)")
        return ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=30_000)
            text = page.inner_text("body")
            browser.close()
    except Exception as exc:
        log.error("JD fetch failed for %s: %s", url, exc)
        return ""
    text = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
    log.info("fetched JD from %s (%d chars)", url, len(text))
    return text[:_MAX]
