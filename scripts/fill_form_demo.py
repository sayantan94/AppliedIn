#!/usr/bin/env python
"""Fill a real application form with the real code path — no LLM, and NO submit.

Proves the deterministic layer end to end: find the form (even when embedded),
map the owner's approved answers onto its fields by label, type them as a person
would, attach the résumé — then stop and screenshot. Submit is never called.

Answer matching here is deliberately plain word-overlap against the answer bank.
The pipeline normally has a model do this mapping; doing it without one keeps this
script free to run, and it is a stricter test — if a field can be matched by
overlap alone, the model will certainly manage it.

Usage:  .venv/bin/python scripts/fill_form_demo.py [url]
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from playwright.async_api import async_playwright  # noqa: E402

from core.stores import make_stores  # noqa: E402
from tools.browser_apply import (  # noqa: E402
    _READ_FORM_JS,
    _fill_human,
    _fix_fields,
    _set_choices,
    _form_frame,
    _set_resume_on_page,
)

DEFAULT_URL = ("https://www.databricks.com/company/careers/engineering---pipeline/"
               "senior-software-engineer---fullstack-6544403002?gh_jid=6544403002")

OUT = Path("/private/tmp/claude-501/-Users-sayantan/"
           "36533b0e-a43f-4ab9-b74b-1d1c323f1e31/scratchpad/filled_form.png")

_STOP = {"the", "your", "you", "please", "a", "an", "of", "to", "for", "and", "or",
         "is", "in", "on", "this", "that", "what", "which", "are", "will", "do",
         "does", "have", "with", "any", "if", "we", "us", "our", "at", "be", "it"}


def _toks(s: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (s or "").lower())
            if len(t) > 2 and t not in _STOP}


def _match(label: str, facts: dict) -> str:
    """The best approved answer for this field label, or ''."""
    lt = _toks(label)
    if not lt:
        return ""
    best, score = "", 0.0
    for question, answer in facts.items():
        qt = _toks(question)
        if not qt:
            continue
        overlap = len(lt & qt) / max(len(qt), 1)
        if overlap > score:
            best, score = answer, overlap
    return best if score >= 0.6 else ""


async def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    stores = make_stores()
    facts = stores.answer_bank.all_facts("Databricks")

    resume = Path(".local/artifacts/resumes/databricks#6544403002.pdf")
    resume_path = str(resume.resolve()) if resume.exists() else ""

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 1600})
        try:
            print(f"opening {url[:90]}…")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(6000)

            frame = await _form_frame(page)
            print(f"form frame: {'EMBEDDED iframe' if frame is not page else 'the page itself'}")

            fields = await frame.evaluate(_READ_FORM_JS)
            fillable = [f for f in fields
                        if f.get("type") not in ("file", "submit", "button", "checkbox", "radio")]
            print(f"fields on the form: {len(fields)}  ({len(fillable)} typeable)\n")

            # What the model supplies in a real run: names split out of "Full name",
            # and a direct yes/no for the authorization questions. Word-overlap
            # alone cannot do either, and testing without them would only be
            # testing this script's matcher rather than the fill path.
            full = str(facts.get("Full name") or "").split()
            derived = {
                "first name": full[0] if full else "",
                "preferred first name": full[0] if full else "",
                "last name": full[-1] if len(full) > 1 else "",
                "location (city)": "Seattle, Washington",
                "are you legally authorized": "Yes",
                "need sponsorship": "Yes",
                "worked for databricks": "No",
                "country": "United States",
            }

            def _answer(label: str) -> str:
                low = label.lower()
                for key, val in derived.items():
                    if key in low:
                        return val
                return _match(label, facts)

            mapping, unmatched = {}, []
            for f in fillable:
                label = str(f.get("label", "")).strip()
                answer = _answer(label)
                (mapping.__setitem__(label, answer) if answer else unmatched.append(label))

            print("MAPPED FROM YOUR KB")
            for k, v in mapping.items():
                print(f"   {k[:44]:46s} <- {str(v)[:38]}")
            if unmatched:
                print("\nNO APPROVED ANSWER (the model would draft/gate these)")
                for u in unmatched[:10]:
                    print(f"   {u[:70]}")

            print("\ntyping…")
            report = await _fill_human(frame, mapping)
            filled = [r.split(": ", 1)[1] for r in report if r.startswith("FILLED:")]
            missed = [r.split(": ", 1)[1] for r in report if r.startswith("NOT FOUND:")]
            print(f"   filled {len(filled)}/{len(mapping)}")
            if missed:
                print(f"   not found: {missed}")

            # CHOICE PASS — radio/checkbox GROUPS (sanctions confirmations,
            # consents). These are required on many forms and cannot be typed.
            choice_map = {}
            for f in fields:
                if f.get("type") != "choice-group":
                    continue
                label = str(f.get("label", ""))
                opts = [str(o) for o in (f.get("options") or [])]
                none_opt = next((o for o in opts if "none of the above" in o.lower()), "")
                if none_opt:                       # sanctions-style confirmation
                    choice_map[label] = none_opt
            if choice_map:
                print(f"\nchoice pass on {len(choice_map)}: "
                      f"{[(k[:34], v[:24]) for k, v in choice_map.items()]}")
                await _set_choices(frame, choice_map, "", url)

            # COMBOBOX PASS — the same one the pipeline runs after the bulk fill.
            # React combobox widgets ignore a typed value: they need a real
            # click, then the option picked out of the popup list. Without this
            # pass every dropdown stays on "Select..." and the form cannot submit.
            combos = {f["label"]: mapping[f["label"]] for f in fields
                      if (f.get("combo") or "location" in str(f.get("label", "")).lower())
                      and f.get("type") not in ("choice-group",)
                      and f.get("label") in mapping}
            if combos:
                combos = {k: (v.split(",")[0].strip() if "location" in k.lower() else v)
                          for k, v in combos.items()}
                print(f"\ncombobox pass on {len(combos)}: {list(combos)[:6]}")
                await _fix_fields(frame, combos, "", url)

            if resume_path:
                n = await _set_resume_on_page(frame, resume_path)
                print(f"   résumé attached to {n} input(s): {Path(resume_path).name}")

            await page.wait_for_timeout(1500)
            # Read the values BACK off the page — proof they actually landed.
            after = await frame.evaluate(_READ_FORM_JS)
            live = {str(f.get("label", "")): str(f.get("value", ""))
                    for f in after if str(f.get("value", "")).strip()}
            print("\nVALUES NOW ON THE PAGE")
            for k, v in list(live.items())[:14]:
                print(f"   {k[:44]:46s} = {v[:40]}")

            OUT.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(OUT), full_page=True)
            print(f"\nscreenshot: {OUT}")
            print("NOT submitted — no submit control was clicked.")
        finally:
            await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
