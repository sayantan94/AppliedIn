#!/usr/bin/env python
"""Survey every watchlist company's application form — no LLM, no cost.

For each company: resolve its ATS, pull one real posting from the board feed,
open the application, and record what the apply pipeline would be up against —
whether the form is embedded in an iframe, how many fields and file inputs it
has, how many are comboboxes (which need the click-and-pick pass rather than
typing), whether a sanctions question is present, and whether a login wall
blocks the form entirely.

Writes a machine-readable summary and, for anything that deviates from its ATS's
normal shape, a starter site-quirks file — so the awkward employers are recorded
instead of rediscovered one failed apply at a time.

Usage:
    .venv/bin/python scripts/survey_company_forms.py            # every company
    .venv/bin/python scripts/survey_company_forms.py Databricks Cohere
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from discovery.resolver import resolve  # noqa: E402
from discovery.watchlist import load_watchlist  # noqa: E402
from tools.browser_apply import _READ_FORM_JS, _form_frame  # noqa: E402

OUT_JSON = ROOT / ".local" / "form_survey.json"
QUIRK_DIR = ROOT / "src" / "agent" / "skills" / "site-quirks" / "companies"

CONCURRENCY = 6
PAGE_TIMEOUT = 20000
PER_COMPANY_TIMEOUT = 75   # seconds; a slow site must not stall the survey

SANCTION_WORDS = ("cuba", "north korea", "sanction", "export control")
LOGIN_WORDS = ("sign in", "log in", "create an account", "create account")


def _one_job_url(ats: str, board: str) -> str:
    """One real posting for this company, straight from the board API."""
    try:
        with httpx.Client(headers={"User-Agent": "Mozilla/5.0"}, timeout=20,
                          follow_redirects=True) as c:
            if ats == "greenhouse":
                jobs = c.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
                             ).json().get("jobs", [])
                return jobs[0].get("absolute_url", "") if jobs else ""
            if ats == "ashby":
                jobs = c.get("https://api.ashbyhq.com/posting-api/job-board/"
                             f"{board}").json().get("jobs", [])
                return jobs[0].get("jobUrl", "") if jobs else ""
            if ats == "lever":
                jobs = c.get(f"https://api.lever.co/v0/postings/{board}?mode=json").json()
                return jobs[0].get("hostedUrl", "") if jobs else ""
    except Exception:  # noqa: BLE001 — a company without a reachable board
        return ""
    return ""


async def survey_one(browser, company, sem) -> dict:  # noqa: ANN001
    name = company.name
    row = {"company": name, "ats": "", "job_url": "", "status": "",
           "embedded": False, "fields": 0, "file_inputs": 0, "combos": 0,
           "choice_groups": 0, "sanctions": False, "login_wall": False}
    async with sem:
        try:
            with httpx.Client(headers={"User-Agent": "Mozilla/5.0"}, timeout=25,
                              follow_redirects=True) as c:
                match = resolve(company.careers_url or "", c, name=name)
            row["ats"] = match.ats
            url = _one_job_url(match.ats, match.board)
            if not url:
                row["status"] = "no feed (crawl-only)"
                return row
            row["job_url"] = url

            page = await browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
                await page.wait_for_timeout(5000)
                # Reach the form the way the pipeline does.
                for sel in ('role=tab[name*="Application"]', 'a:has-text("Apply for this job")',
                            'a:has-text("Apply now")', 'button:has-text("Apply")'):
                    try:
                        await page.locator(sel).first.click(timeout=2000)
                        await page.wait_for_timeout(1500)
                        break
                    except Exception:  # noqa: BLE001
                        continue

                frame = await _form_frame(page)
                row["embedded"] = frame is not page
                fields = await frame.evaluate(_READ_FORM_JS)
                row["fields"] = len(fields)
                row["combos"] = sum(1 for f in fields if f.get("combo"))
                row["choice_groups"] = sum(1 for f in fields
                                           if f.get("type") == "choice-group")
                row["file_inputs"] = await frame.evaluate(
                    "() => document.querySelectorAll('input[type=file]').length")
                body = (await frame.evaluate("() => document.body.innerText || ''")).lower()
                row["sanctions"] = any(w in body for w in SANCTION_WORDS)
                row["login_wall"] = (row["fields"] < 3
                                     and any(w in body for w in LOGIN_WORDS))
                row["status"] = ("login wall" if row["login_wall"]
                                 else "form found" if row["fields"] >= 3
                                 else "no form found")
            finally:
                await page.close()
        except Exception as exc:  # noqa: BLE001
            row["status"] = f"error: {type(exc).__name__}"
    return row


QUIRK_TEMPLATE = """---
name: {name}
match_companies: [{slug}]
---

{body}
"""


def write_quirk(row: dict) -> str | None:
    """A starter quirk file for a company whose form needs special handling."""
    notes = []
    if row["embedded"]:
        notes.append(
            "- The application is served in an **iframe**, so the job page itself has no "
            "form fields and no résumé input. Work inside the frame whose URL belongs to "
            f"the ATS ({row['ats'] or 'the board'}); reading the top-level document finds "
            "only the site's own nav and cookie controls.")
    if row["combos"] >= 3:
        notes.append(
            f"- {row['combos']} of its fields are combobox widgets, not text boxes. They "
            "ignore a typed value: click, wait for the option list, then click the option. "
            "Typing and moving on leaves them empty and the form will not submit.")
    if row["sanctions"]:
        notes.append(
            "- Carries an export-control / sanctions question. The answer is always the "
            "negative one (\"None of the above\"). Never tick a country option.")
    if row["login_wall"]:
        notes.append(
            "- The form is behind a sign-in wall. Without a saved session, stop and report "
            "that a login is needed — do not create an account.")
    if not notes:
        return None
    slug = row["company"].lower().replace(" ", "-")
    path = QUIRK_DIR / f"{slug}.md"
    if path.exists():
        return None  # never clobber a hand-written note
    path.write_text(QUIRK_TEMPLATE.format(
        name=row["company"], slug=row["company"].lower(), body="\n".join(notes)))
    return path.name


async def main() -> int:
    wanted = {a.lower() for a in sys.argv[1:]}
    companies = [c for c in load_watchlist(ROOT / "config" / "watchlist.yaml")
                 if c.careers_url and (not wanted or c.name.lower() in wanted)]
    print(f"surveying {len(companies)} companies ({CONCURRENCY} at a time)…\n")

    sem = asyncio.Semaphore(CONCURRENCY)
    done = 0

    async def _guarded(browser, company):  # noqa: ANN001
        nonlocal done
        try:
            row = await asyncio.wait_for(survey_one(browser, company, sem),
                                         timeout=PER_COMPANY_TIMEOUT)
        except asyncio.TimeoutError:
            row = {"company": company.name, "ats": "", "job_url": "", "status": "timeout",
                   "embedded": False, "fields": 0, "file_inputs": 0, "combos": 0,
                   "choice_groups": 0, "sanctions": False, "login_wall": False}
        done += 1
        print(f"  [{done:>2}/{len(companies)}] {row['company'][:22]:<24}"
              f"{row['ats'][:11]:<12} {row['status']}", flush=True)
        return row

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            rows = await asyncio.gather(*(_guarded(browser, c) for c in companies))
        finally:
            await browser.close()

    rows.sort(key=lambda r: (r["status"] != "form found", r["company"]))
    print(f"{'COMPANY':<20}{'ATS':<13}{'FIELDS':>7}{'FILE':>5}{'COMBO':>6}"
          f"{'GRP':>4}  {'EMBED':<6}{'SANCT':<6}STATUS")
    for r in rows:
        print(f"{r['company'][:19]:<20}{r['ats'][:12]:<13}{r['fields']:>7}"
              f"{r['file_inputs']:>5}{r['combos']:>6}{r['choice_groups']:>4}  "
              f"{'yes' if r['embedded'] else '-':<6}{'yes' if r['sanctions'] else '-':<6}"
              f"{r['status']}")

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(rows, indent=2))

    QUIRK_DIR.mkdir(parents=True, exist_ok=True)
    written = [w for w in (write_quirk(r) for r in rows if r["status"] == "form found") if w]
    print(f"\nsurvey -> {OUT_JSON}")
    print(f"quirk files written: {len(written)}"
          + (f" ({', '.join(written)})" if written else ""))
    ok = sum(1 for r in rows if r["status"] == "form found")
    print(f"forms reachable: {ok}/{len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
