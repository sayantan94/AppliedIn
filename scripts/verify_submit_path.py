#!/usr/bin/env python
"""End-to-end proof of the SUBMIT path — no LLM, and no real employer touched.

The other scripts stop before the button, so the last unproven step was the one
that matters most: click submit, then decide whether an application really went
through. Firing a live application at a company to test that is not an option —
it cannot be undone and it is sent under the owner's name. So this serves a
local replica of the hard case (a Greenhouse-style form embedded in an iframe,
with required text fields, comboboxes, a résumé upload and a sanctions
checkbox group) and drives the real pipeline helpers against it.

Because the fixture is the server, it can assert the thing a live run never
could: exactly which values arrived. A form that looks filled on screen but
posts empty strings would pass a screenshot check and fail here.

Checks:
  1. Full fill + submit  -> confirmation detected, and the server received every
                            required value, with the sanctions answer negative.
  2. Submit rejected     -> NOT recorded as applied (the form is still asking).
  3. Browser error page  -> NOT recorded as applied.

Usage:  .venv/bin/python scripts/verify_submit_path.py
Exit code 0 = the submit path works.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from playwright.async_api import async_playwright  # noqa: E402

from tools.browser_apply import (  # noqa: E402
    _READ_FORM_JS,
    _applied_signal,
    _ensure_sanctions_safe,
    _fill_human,
    _fix_fields,
    _form_frame,
    _set_choices,
    _set_resume_on_page,
)

RECEIVED: dict = {}

JOB_PAGE = """<!doctype html><html><body>
<h1>Senior Software Engineer</h1><p>Seattle, Washington</p>
<div id="cookie-banner" class="cookie-banner">
  <label><input type="checkbox" name="perf"> Performance Cookies</label>
  <label><input type="checkbox" name="targ"> Targeting Cookies</label>
  <button>Accept All</button>
</div>
<iframe src="/embed" style="width:900px;height:1400px;border:0"></iframe>
</body></html>"""

FORM_PAGE = """<!doctype html><html><body>
<h2>Apply for this job</h2>
<form method="POST" action="/submit">
  <label>First Name*<input type="text" name="first" required></label>
  <label>Last Name*<input type="text" name="last" required></label>
  <label>Email*<input type="email" name="email" required></label>
  <label>Phone*<input type="tel" name="phone" required></label>
  <label>Location (City)*<input type="text" name="loc" role="combobox"
         aria-autocomplete="list" placeholder="Start typing…" required></label>
  <label>Resume/CV*<input type="file" name="resume" accept=".pdf,.doc,.docx" required></label>
  <label>Cover Letter<input type="file" name="cover" accept=".pdf,.doc,.docx"></label>
  <label>Are you legally authorized to work in the country in which you are applying?*
    <select name="auth" required>
      <option value="">Select...</option><option>Yes</option><option>No</option></select></label>
  <fieldset><legend>Please confirm whether any of the below applies to you.
      Note: U.S. sanctions and export controls.*</legend>
    <label><input type="checkbox" name="s1"> Citizen or permanent resident of Cuba, Iran, North Korea, or Syria</label>
    <label><input type="checkbox" name="s2"> Ordinarily a resident of Russia or Belarus</label>
    <label><input type="checkbox" name="none"> None of the above</label>
  </fieldset>
  <button type="submit">Submit application</button>
</form></body></html>"""

CONFIRM_PAGE = """<!doctype html><html><body>
<h1>Thank you for applying</h1>
<p>We have received your application and will be in touch.</p>
</body></html>"""

REJECT_PAGE = """<!doctype html><html><body>
<h2>Apply for this job</h2>
<p>Thank you for applying — please correct the errors below.</p>
<form method="POST" action="/submit">
  <label>First Name*<input type="text" name="first" required></label>
  <button type="submit">Submit application</button>
</form></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: ANN001, ANN002 — keep the output clean
        pass

    def _send(self, body: str, code: int = 200) -> None:
        raw = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/embed"):
            self._send(FORM_PAGE)
        elif self.path.startswith("/reject"):
            self._send(REJECT_PAGE)
        else:
            self._send(JOB_PAGE)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        # multipart (file upload) — pull the plain fields out of the parts
        fields: dict[str, str] = {}
        if "multipart/form-data" in (self.headers.get("Content-Type") or ""):
            for part in raw.split("------"):
                if 'name="' not in part:
                    continue
                key = part.split('name="', 1)[1].split('"', 1)[0]
                val = part.split("\r\n\r\n", 1)[1].rsplit("\r\n", 1)[0] if "\r\n\r\n" in part else ""
                if "filename=" in part:
                    val = part.split('filename="', 1)[1].split('"', 1)[0] or "(file)"
                fields[key] = val.strip()
        else:
            fields = {k: (v[0] if v else "") for k, v in parse_qs(raw).items()}
        RECEIVED.clear()
        RECEIVED.update(fields)
        self._send(CONFIRM_PAGE)


def serve() -> tuple[HTTPServer, str]:
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
        if not ok:
            self.failures.append(label)


async def full_submit(browser, base: str, rep: Report) -> None:  # noqa: ANN001
    print("\n1. fill an embedded form, click Submit, read the outcome")
    page = await browser.new_page()
    try:
        await page.goto(f"{base}/job", wait_until="domcontentloaded")
        await page.wait_for_timeout(400)
        frame = await _form_frame(page)
        rep.check(frame is not page, "works inside the embedded form")

        fields = await frame.evaluate(_READ_FORM_JS)
        mapping = {"First Name*": "Alex", "Last Name*": "Rivera",
                   "Email*": "alex.rivera@example.com", "Phone*": "5551234567",
                   "Location (City)*": "Seattle"}
        await _fill_human(frame, mapping)
        await _fix_fields(frame, {"Are you legally authorized to work in the country "
                                  "in which you are applying?*": "Yes"}, "", base)
        # The sanctions group: propose the WRONG answer on purpose. The guard in
        # _set_choices must convert it to the negative one before anything is clicked.
        sanctions_q = next((str(f.get("label")) for f in fields
                            if "sanctions" in str(f.get("label", "")).lower()), "")
        await _set_choices(frame, {sanctions_q: "Citizen or permanent resident of "
                                                "Cuba, Iran, North Korea, or Syria"}, "", base)
        # The unconditional sweep: it must answer the question even though the
        # reader never grouped it, and never tick a country option.
        note = await _ensure_sanctions_safe(frame)
        rep.check("None of the above" in note, "sanctions sweep answered the question", note)

        resume = Path(__file__).with_name("_fixture_resume.pdf")
        resume.write_bytes(b"%PDF-1.4\n%%EOF\n")
        n = await _set_resume_on_page(frame, str(resume))
        rep.check(n == 1, "résumé on the résumé field only (not Cover Letter)",
                  f"{n} input(s)")

        url_before = page.url
        await frame.locator('button[type="submit"]').click()
        await page.wait_for_timeout(800)

        signal = await _applied_signal(page, url_before, allow_vision=False)
        rep.check(bool(signal), "confirmation detected after submit", str(signal))

        # What the SERVER actually received — the part a screenshot cannot show.
        rep.check(RECEIVED.get("first") == "Alex" and RECEIVED.get("last") == "Rivera",
                  "server received the name", f"{RECEIVED.get('first')} {RECEIVED.get('last')}")
        rep.check(RECEIVED.get("email") == "alex.rivera@example.com",
                  "server received the email", RECEIVED.get("email", ""))
        rep.check(RECEIVED.get("auth") == "Yes",
                  "server received the authorization answer", RECEIVED.get("auth", ""))
        rep.check(str(RECEIVED.get("resume", "")).endswith(".pdf"),
                  "server received the résumé file", RECEIVED.get("resume", ""))
        rep.check("cover" not in RECEIVED or not RECEIVED.get("cover"),
                  "no cover letter was sent", RECEIVED.get("cover", "(none)"))
        rep.check("s1" not in RECEIVED and "s2" not in RECEIVED,
                  "NO sanctioned-country box was ticked",
                  "clean" if "s1" not in RECEIVED else f"LEAKED {RECEIVED.get('s1')}")
        rep.check(RECEIVED.get("none") is not None,
                  "'None of the above' was ticked instead")
        resume.unlink(missing_ok=True)
    finally:
        await page.close()


async def rejected_submit(browser, base: str, rep: Report) -> None:  # noqa: ANN001
    print("\n2. the form comes BACK still asking — must not read as applied")
    page = await browser.new_page()
    try:
        await page.goto(f"{base}/reject", wait_until="domcontentloaded")
        await page.wait_for_timeout(300)
        # The page literally says "Thank you for applying" but still wants input.
        signal = await _applied_signal(page, f"{base}/reject", allow_vision=False)
        rep.check(signal is None, "success wording ignored while input is pending",
                  f"got {signal!r}")
    finally:
        await page.close()


async def error_page(browser, base: str, rep: Report) -> None:  # noqa: ANN001
    print("\n3. a page that never loaded — must not read as applied")
    page = await browser.new_page()
    try:
        await page.set_content("<body>This site can't be reached<br>"
                               "DNS_PROBE_FINISHED_NXDOMAIN</body>")
        signal = await _applied_signal(page, base, allow_vision=False)
        rep.check(signal is None, "browser error page is not a confirmation",
                  f"got {signal!r}")
    finally:
        await page.close()


async def main() -> int:
    srv, base = serve()
    rep = Report()
    print(f"local fixture serving on {base} (no real employer is contacted)")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            await full_submit(browser, base, rep)
            await rejected_submit(browser, base, rep)
            await error_page(browser, base, rep)
        finally:
            await browser.close()
            srv.shutdown()
    print("\nwhat the server received:")
    print("  " + json.dumps(RECEIVED, indent=2).replace("\n", "\n  "))
    print("\n" + ("SUBMIT PATH VERIFIED" if not rep.failures
                  else f"{len(rep.failures)} FAILED: {rep.failures}"))
    return 0 if not rep.failures else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
