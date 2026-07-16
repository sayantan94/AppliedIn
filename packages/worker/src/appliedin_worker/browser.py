"""Real-browser launch — used ONLY in the Fargate container.

Playwright is imported lazily so tests (which always inject fake pages) run
without it installed. The container image bakes in Chromium (see Dockerfile).
"""

from __future__ import annotations

from typing import Any


async def launch_page() -> Any:
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    return await browser.new_page()
