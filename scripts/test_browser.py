"""Standalone browser-use apply tester — iterate on the browser tool in isolation
(no daemon, no queue, no scripted engine). Runs browser-use END TO END and, given
several URLs for the SAME company, reuses ONE persistent Chrome session so a
CAPTCHA/login you solve once carries to the rest.

  PYTHONPATH=src .venv/bin/python scripts/test_browser.py <url> [url2 ...] [company]

Config follows the Fable advisor's source-verified recommendations for browser-use
0.5.9: keep_alive + shared BrowserSession, viewport_expansion=-1, a deterministic
select_autocomplete action (Ashby Location), an on_step_start CAPTCHA gate, and an
anti-loop task. Solve CAPTCHAs in the visible window; press Enter when prompted.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from urllib.parse import urlparse

import core.config  # noqa: F401 — loads .env + sets litellm.drop_params
from core.config import get_settings
from core.stores import make_stores
from tools.browser_apply import (
    _APPLY_POLICY,
    _apply_controller,
    _clean_resume_copy,
    _is_text_only,
    _step_cb,
)
from tools.browser_llm import make_llm


class LoopBreak(RuntimeError):
    def __init__(self, msg: str, force_submit: bool = False):
        super().__init__(msg)
        self.force_submit = force_submit


def make_loop_guard():
    """Break the re-fill loop FAST for ANY filling action: two zero-progress calls
    (SET 0/… or NOTHING TO DO, or an identical result) → stop and FORCE a
    deterministic submit rather than aborting a filled form. Fresh per job. API
    verified against browser-use 0.5.9."""
    import re as _re
    _WATCH = ("fill_fields", "select_choices", "verify_form_filled")
    st = {"last": {}, "streak": {}}

    async def guard(agent) -> None:  # noqa: ANN001
        hist = agent.history.history
        if not hist or not hist[-1].model_output:
            return
        act = hist[-1].model_output.action[0].model_dump(exclude_none=True)
        name = next((k for k in act if k != "interacted_element"), "")
        if name not in _WATCH:
            return
        res = (hist[-1].result or [None])[0]
        mem = (res.long_term_memory or res.extracted_content or "") if res else ""
        sig = _re.sub(r"\d+", "", mem)[:400]  # digit-stripped: 'verify #2' == 'verify #3'
        zero = mem.startswith(f"{name} SET 0/") or "NOTHING TO DO" in mem or "EXHAUSTED" in mem
        same = bool(st["last"].get(name)) and sig == st["last"][name]
        st["streak"][name] = st["streak"].get(name, 0) + 1 if (zero or same) else 0
        st["last"][name] = sig
        if "NOTHING TO DO" in mem or st["streak"].get(name, 0) >= 2:
            agent.stop()
            raise LoopBreak(f"{name} made no progress twice — forcing deterministic submit",
                            force_submit=True)

    return guard


TASK = """Complete AND SUBMIT this {company} job application, END TO END. URL: {url}
{resume_line}Approved answers — type these VERBATIM, never invent:
{facts}

Do each phase ONCE, in order:
1. upload_resume (if a résumé field exists) — confirm the filename shows.
2. fill_fields with ALL text/dropdown/textarea fields in ONE call. For any field that
   shows SUGGESTIONS while typing (Location, School, Company), use select_autocomplete
   — never fill_fields for those. select_choices for ALL radio/checkbox questions.
3. draft_essay_answer for each open-ended question with no approved answer; use it verbatim.
4. verify_form_filled. If it lists problems, fix ONLY those fields, then verify again.
   Max 2 repair rounds — after 2, proceed anyway.
5. Click Submit / Submit Application. Wait. Success = a confirmation ('submitted',
   'thank you', a confirmation number) or a URL change. If validation errors appear,
   do ONE repair round from those messages and submit again.
6. Finish with:  APPLIED: <the exact confirmation text>

RULES:
- Never re-fill a field verify_form_filled reported OK, even if a screenshot looks empty
  (the form re-renders — TRUST verify, not pixels).
- If select_autocomplete errors, retry ONCE with a shorter prefix (e.g. 'San Franc'), then move on.
- If the same action fails 3 times, or submit fails twice: finish with  UNCERTAIN: <error text>. Do not keep retrying.
- A CAPTCHA or login wall: call ask_human_to_unblock ONCE, then continue. Never refresh to bypass.
- Never invent a value; a required field with no answer -> finish with  MISSING: <question>.
- fields_json keys MUST be copied EXACTLY from a fill_fields receipt or verify_form_filled — never paraphrase a label. A key marked UNMATCHABLE must NEVER be sent to fill_fields again.
"""

_CAPTCHA_JS = """() => {
  const f = [...document.querySelectorAll('iframe[src*="hcaptcha"],iframe[src*="recaptcha"],iframe[src*="turnstile"],iframe[src*="challenge"]')]
    .find(el => { const r = el.getBoundingClientRect();
                  return r.width > 150 && r.height > 60 && el.offsetParent !== null; });
  return !!f;
}"""


def _find_resume_pdf() -> str:
    cands: list = []
    for d in (".local/artifacts/resumes", "output", "resume"):
        p = Path(d)
        if p.exists():
            cands += list(p.rglob("*.pdf"))
    cands.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return str(cands[0]) if cands else ""


def _build_profile(company: str, allowed: list):  # noqa: ANN201
    from browser_use import BrowserProfile
    base = dict(
        headless=False, channel="chrome", keep_alive=True,
        user_data_dir=str(Path(f".local/chrome-profiles/{company}").resolve()),
        allowed_domains=allowed, wait_between_actions=1.0,
        minimum_wait_page_load_time=0.5, wait_for_network_idle_page_load_time=1.5,
        maximum_wait_page_load_time=8.0, viewport_expansion=-1)
    for kw in (base, {k: base[k] for k in ("headless", "channel", "keep_alive",
                                           "user_data_dir", "allowed_domains")}):
        try:
            return BrowserProfile(**kw)
        except Exception as exc:  # noqa: BLE001
            print(f"  (BrowserProfile fell back: {exc})")
    return BrowserProfile(headless=False)


def _extra_actions(controller) -> None:  # noqa: ANN001
    """Add the deterministic autocomplete filler + human-handoff to our controller."""
    from browser_use import ActionResult

    @controller.registry.action(
        "Fill a typeahead/autocomplete field (Location, School, Company) and pick the "
        "best suggestion. ALWAYS use this for fields that show suggestions while typing "
        "— never fill_fields for those.")
    async def select_autocomplete(field_label: str, text: str, page) -> ActionResult:  # noqa: ANN001
        from tools.browser_apply import _LOCATE_INPUT_JS
        try:
            handle = await page.evaluate_handle(_LOCATE_INPUT_JS, field_label)
            inp = handle.as_element()  # get_by_label silently fails on Ashby — locate by resolved label
            if inp is None:
                return ActionResult(error=f'{field_label}: input not found on page')
            await inp.click(timeout=3000)
            await inp.fill("")
            await inp.type(text.split(",")[0].strip(), delay=80)  # city token, real keystrokes
            await page.wait_for_timeout(1000)
            city = text.split(",")[0].strip()
            for sel in ('[role="option"]', '[role="listbox"] button',
                        f'button:has-text("{city}")', f'li:has-text("{city}")'):
                opts = page.locator(sel)
                if await opts.count():
                    await opts.first.click()
                    return ActionResult(extracted_content=f'{field_label} = "{await inp.input_value()}"')
            await inp.press("ArrowDown")
            await page.wait_for_timeout(300)
            await inp.press("Enter")
            val = await inp.input_value()
            if val and city.lower() in val.lower():
                return ActionResult(extracted_content=f'{field_label} = "{val}" (keyboard)')
            return ActionResult(error=f'{field_label}: no suggestion matched "{city}" — retry a shorter prefix')
        except Exception as exc:  # noqa: BLE001
            return ActionResult(error=f'{field_label}: autocomplete failed ({exc})')

    @controller.registry.action(
        "Hand off to the human ONLY when a CAPTCHA challenge or a login/account wall "
        "blocks ALL progress. Never try to solve a CAPTCHA yourself.")
    async def ask_human_to_unblock(reason: str) -> ActionResult:
        print(f"\n>>> HUMAN NEEDED: {reason}\n    Resolve it in the Chrome window, then press Enter here…")
        await asyncio.get_event_loop().run_in_executor(None, input)
        return ActionResult(
            extracted_content=f"Human resolved: {reason}. The blocker is gone — continue the form.",
            long_term_memory=f"Human already resolved: {reason}. Do NOT call ask_human_to_unblock for this again.")


async def _gate_on_captcha(agent) -> None:  # noqa: ANN001
    try:
        page = await agent.browser_session.get_current_page()
        if await page.evaluate(_CAPTCHA_JS):
            print("\n>>> CAPTCHA detected — solve it in the Chrome window, then press Enter here…")
            await asyncio.get_event_loop().run_in_executor(None, input)
    except Exception:  # noqa: BLE001
        pass


async def run_company(company: str, urls: list) -> None:
    from browser_use import Agent, BrowserSession

    s = get_settings()
    facts = make_stores().answer_bank.all_facts(company)
    facts_str = "\n".join(f"- {q}: {a}" for q, a in facts.items() if a) or "(none)"
    resume_pdf = _find_resume_pdf()
    upload_path = _clean_resume_copy(resume_pdf, facts.get("Full name") or "Sayantan Bhowmik") if resume_pdf else ""
    resume_line = ("This application REQUIRES a résumé — call upload_resume (NOT the "
                   "'Autofill from resume' box).\n" if upload_path else "")
    model = s.browser_model
    host = urlparse(urls[0]).hostname or ""
    allowed = list({host, f"*.{'.'.join(host.split('.')[-2:])}" if host else ""} - {""})

    print(f"\n=== browser-use e2e · {company} · {len(urls)} job(s) ===\n"
          f"  model:  {model} (vision={not _is_text_only(model)})\n"
          f"  resume: {upload_path or '(none)'} | facts: {sum(1 for v in facts.values() if v)}\n")

    session = BrowserSession(browser_profile=_build_profile(company, allowed))
    await session.start()  # ONE Chrome for all this company's jobs
    try:
        for i, url in enumerate(urls, 1):
            print(f"\n----- job {i}/{len(urls)}: {url} -----")
            controller = _apply_controller(upload_path, pk=f"TEST{i}", url=url, company=company)
            _extra_actions(controller)
            opts = dict(
                task=TASK.format(company=company, url=url, resume_line=resume_line, facts=facts_str),
                llm=make_llm(model), browser_session=session, controller=controller,
                initial_actions=[{"go_to_url": {"url": url}}],
                use_vision=not _is_text_only(model), max_actions_per_step=1,
                max_failures=5, retry_delay=3, extend_system_message=_APPLY_POLICY,
                available_file_paths=[upload_path] if upload_path else None,
                validate_output=False, register_new_step_callback=_step_cb(f"TEST{i}", url))
            try:
                agent = Agent(max_history_items=24, llm_timeout=90, **opts)
            except TypeError:
                agent = Agent(**opts)  # older signature — drop the optional tuning params
            guard = make_loop_guard()  # fresh per job

            async def _hook(agent) -> None:  # noqa: ANN001
                await _gate_on_captcha(agent)
                await guard(agent)

            try:
                history = await agent.run(max_steps=30, on_step_start=_hook)
                text = (history.final_result() if hasattr(history, "final_result") else str(history)) or ""
            except LoopBreak as lb:
                print(f"\n[loop-guard] {lb}")
                text = str(lb)
                if lb.force_submit:  # form is filled — submit deterministically, don't abort
                    from tools.browser_apply import _finalize_submit
                    try:
                        page = await session.get_current_page()
                        res = await _finalize_submit(page, facts, {}, f"TEST{i}", url, headed=True)
                        text = f"forced submit → {res.get('status')}: {str(res.get('detail') or res.get('question') or '')[:300]}"
                    except Exception as exc:  # noqa: BLE001
                        text = f"forced submit errored: {exc}"
            print(f"\n=== job {i} RESULT ===\n{text[:1500] or '(no final text)'}")
    finally:
        try:
            input("\n[all jobs done — browser left open; press Enter to close] ")
        except Exception:  # noqa: BLE001
            pass
        try:
            await session.kill()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a]
    if not args:
        print("usage: test_browser.py <url> [url2 ...] [company]")
        sys.exit(1)
    urls = [a for a in args if a.startswith("http")]
    tail = [a for a in args if not a.startswith("http")]
    if not urls:
        print("give at least one http(s) job URL")
        sys.exit(1)
    company = tail[0] if tail else (
        urls[0].split("/")[3] if "ashbyhq.com" in urls[0]
        else (urlparse(urls[0]).hostname or "").split(".")[0])
    asyncio.run(run_company(company, urls))
