#!/usr/bin/env python
"""Verify the apply pipeline targets the REAL application form — no LLM, no cost.

Drives the same DOM helpers the pipeline uses (`_form_frame`, `_READ_FORM_JS`,
`_fill_human`, `_set_resume_on_page`) against live postings with Playwright only.
No model is ever called, so this can be run as often as you like.

It checks the three failures we actually hit:

  1. An EMBEDDED form (Greenhouse in an iframe) is found, not the employer page.
  2. Cookie-banner controls are invisible to the reader and the filler — the
     pipeline was toggling "Performance Cookies"/"Targeting Cookies" while the
     real form sat untouched.
  3. A directly-hosted form (Ashby) is unaffected by the frame switch.

Usage:  .venv/bin/python scripts/verify_form_targeting.py
Exit code 0 = all checks passed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from playwright.async_api import async_playwright  # noqa: E402

from tools.browser_apply import (  # noqa: E402
    _READ_FORM_JS,
    _fill_human,
    _form_frame,
    _set_resume_on_page,
)

EMBEDDED = ("Databricks (Greenhouse embed)",
            "https://www.databricks.com/company/careers/engineering---pipeline/"
            "senior-software-engineer---fullstack-6544403002?gh_jid=6544403002")
DIRECT = ("Cohere (Ashby, hosted directly)",
          "https://jobs.ashbyhq.com/cohere/1fa01a03-9253-4f62-8f10-0fe368b38cb9/application")

COOKIE_WORDS = ("cookie", "targeting", "performance cookies", "functional cookies",
                "tothr", "optanon", "onetrust")


def _looks_like_cookie(label: str) -> bool:
    low = (label or "").lower()
    return any(w in low for w in COOKIE_WORDS)


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
        if not ok:
            self.failures.append(label)


async def _resume_file() -> str:
    import tempfile
    d = Path(tempfile.mkdtemp()) / "Test Resume.pdf"
    d.write_bytes(b"%PDF-1.4\n%%EOF\n")
    return str(d)


async def check_embedded(browser, rep: Report) -> None:
    name, url = EMBEDDED
    print(f"\n{name}")
    page = await browser.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(6000)

        top_fields = await page.evaluate(_READ_FORM_JS)
        frame = await _form_frame(page)
        rep.check(frame is not page, "switches into the embedded form frame")

        fields = await frame.evaluate(_READ_FORM_JS)
        labels = [str(f.get("label", "")) for f in fields]
        rep.check(len(fields) > len(top_fields),
                  "reads the real form, not the employer page",
                  f"{len(top_fields)} top-level -> {len(fields)} in frame")

        # The application's own fields must be there.
        wanted = ("first name", "last name", "email")
        found = [w for w in wanted if any(w in lb.lower() for lb in labels)]
        rep.check(len(found) == len(wanted), "finds the application's core fields",
                  f"found {found}")

        # ...and nothing from the cookie banner.
        cookies = [lb for lb in labels if _looks_like_cookie(lb)]
        rep.check(not cookies, "reader ignores cookie-banner controls",
                  f"leaked: {cookies[:4]}" if cookies else "none leaked")

        # The filler must not reach them either, even when asked by name.
        report = await _fill_human(frame, {"Performance Cookies": "yes",
                                           "Targeting Cookies": "yes"})
        filled = [r for r in report if r.startswith("FILLED:")]
        rep.check(not filled, "filler refuses cookie-banner controls",
                  f"would have filled: {filled}" if filled else "refused")

        n = await _set_resume_on_page(frame, await _resume_file())
        rep.check(n > 0, "attaches the résumé", f"{n} file input(s)")
    finally:
        await page.close()


async def check_direct(browser, rep: Report) -> None:
    name, url = DIRECT
    print(f"\n{name}")
    page = await browser.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)
        frame = await _form_frame(page)
        rep.check(frame is page, "stays on the page (no frame switch)")
        fields = await frame.evaluate(_READ_FORM_JS)
        rep.check(len(fields) >= 5, "still reads its form", f"{len(fields)} fields")
    finally:
        await page.close()


async def main() -> int:
    rep = Report()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            await check_embedded(browser, rep)
            await check_direct(browser, rep)
        finally:
            await browser.close()
    print("\n" + ("ALL CHECKS PASSED" if not rep.failures
                  else f"{len(rep.failures)} FAILED: {rep.failures}"))
    return 0 if not rep.failures else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
