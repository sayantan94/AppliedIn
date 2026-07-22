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


def fetch_jd_meta(url: str) -> dict:
    """Return {'title': ..., 'text': ...} for a posting — the page's own title
    (Ashby/Greenhouse set a descriptive <title>) plus the visible body text."""
    if not url:
        return {"title": "", "text": ""}
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {"title": "", "text": ""}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=30_000)
            raw_title = (page.title() or "").strip()
            h1 = ""
            try:
                h1 = (page.locator("h1").first.inner_text(timeout=1500) or "").strip()
            except Exception:
                pass
            text = page.inner_text("body")
            browser.close()
    except Exception as exc:
        log.error("JD meta fetch failed for %s: %s", url, exc)
        return {"title": "", "text": ""}
    # Prefer a clean role title: the h1, else the <title> before any " - Company"
    # / " | Company" / " at Company" separator.
    import re as _re
    title = h1 or _re.split(r"\s[|\-–—]\s|\s+at\s+", raw_title)[0].strip()
    text = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
    return {"title": title[:90], "text": text}


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
