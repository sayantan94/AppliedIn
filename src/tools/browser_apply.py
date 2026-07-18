"""Actual applying, via browser-use (a browser-agent framework).

ADK orchestrates (find → score → tailor → gate/resume); browser-use is the
agent that drives the real browser to fill and submit the application. It gets:
  - the human-approved facts (answer bank + saved login),
  - the TAILORED résumé PDF to upload, and
  - a WRITER model (tools.narrative) that drafts free-text answers ("why this
    role?") from the tailored résumé + GitHub + JD on the fly.

So instead of stopping at the first open-ended question, it drafts a truthful,
compelling answer and keeps going — only gating to the human for a genuine
unknown (a fact/credential we don't have), an account wall, or a CAPTCHA.
"""

from __future__ import annotations

import contextvars
import json

from core.logging import get_logger

log = get_logger(__name__)

# Per-run Chrome profile override. Chrome locks a user_data_dir to a single
# process, so parallel applies (approve-all runs 5 at once) can't share the one
# configured profile. A batch runner sets this contextvar to a distinct dir per
# concurrent worker; a normal single apply leaves it empty and uses the config.
_PROFILE_OVERRIDE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "appliedin_profile_dir", default="")


def set_profile_override(path: str) -> None:
    """Point THIS run's apply browser at a specific profile dir (contextvar-scoped
    to the calling thread/task). Empty string clears it → falls back to config."""
    _PROFILE_OVERRIDE.set(path or "")


def _profile_dir() -> str:
    """The Chrome profile dir for this run: a batch override if set, else config."""
    if ov := _PROFILE_OVERRIDE.get():
        return ov
    from core.config import get_settings
    return (getattr(get_settings(), "browser_profile_dir", "") or "").strip()

_MAX_PASSES = 2   # essays draft IN-RUN now; pass 2 is only a safety net
_MAX_STEPS = 22   # happy path is ~8 steps; a tight budget funds tools, not loops

# System-level policy (better compliance than the task string) — forces the model
# to use our deterministic tools and never wander off the application form.
_APPLY_POLICY = (
    "You are filling ONE job-application form. Hard rules:\n"
    "- Stay on the application page. NEVER click the company logo/name, 'View all "
    "jobs', careers/privacy links, or anything that leaves the form.\n"
    "- Use the provided tools, not manual clicking/typing: upload_resume for the "
    "résumé, fill_fields for ALL text/dropdown/textarea fields at once, "
    "select_choices for ALL radio/checkbox questions, draft_essay_answer for essays "
    "(use its text verbatim — never write essay prose yourself).\n"
    "- Before submit, call verify_form_filled and fix only what's empty.\n"
    "- Claim success ONLY if you can SEE explicit confirmation text ('Thank you for "
    "applying', 'Application submitted', a confirmation number) — quote it. If none "
    "is visible, do NOT claim success."
)


async def apply(url: str, company: str, facts: dict, model: str, *, pk: str = "",
                jd_text: str = "", resume_tex: str = "", github: str = "",
                resume_path: str = "") -> dict:
    """Fill and SUBMIT this application. Returns one of:
      {status:'applied', confirmation}   {status:'gate', reason, question}
      {status:'unknown', detail}

    Engine (config apply_engine): "agent" = browser-use drives the browser (the
    LLM `model`), using our deterministic helper tools for the hard parts (upload,
    radios, fill, essays). "scripted" = a pure-Playwright pipeline, no LLM in the
    click loop. Either way the outcome shape is identical."""
    from core.config import get_settings

    engine = (getattr(get_settings(), "apply_engine", "agent") or "agent").lower()
    if engine != "scripted":  # browser-use is the driver
        return await _agent_apply(url, company, facts, model, pk=pk, jd_text=jd_text,
                                  resume_tex=resume_tex, github=github,
                                  resume_path=resume_path)
    try:
        result = await _scripted_apply(url, company, facts, model, pk=pk,
                                       jd_text=jd_text, resume_tex=resume_tex,
                                       github=github, resume_path=resume_path)
        if result is not None:
            return result
        _emit(pk, "response", agent="browser", url=url,
              detail="scripted flow couldn't run here — switching to agent mode")
    except Exception as exc:
        # If the browser was CLOSED or CRASHED, the form was already opened, filled,
        # and (usually) handed to you at the CAPTCHA — re-running the agent could
        # DOUBLE-SUBMIT. Never do that; report uncertain so you verify the portal.
        if _page_gone(exc):
            log.warning("scripted apply: browser closed/crashed (%s) — NOT re-running", exc)
            _emit(pk, "response", agent="applier", url=url,
                  detail="⚠️ the browser closed before I could confirm — I did NOT resubmit. "
                         "Check the portal; if it went through, mark it applied.")
            return {"status": "uncertain",
                    "detail": "Browser closed/crashed during or after submit — not resubmitted; "
                              "verify on the portal whether the application went through."}
        log.warning("scripted apply errored (%s) — agent fallback", exc)
        _emit(pk, "response", agent="browser", url=url,
              detail=f"scripted flow errored ({exc}) — switching to agent mode")
    return await _agent_apply(url, company, facts, model, pk=pk, jd_text=jd_text,
                              resume_tex=resume_tex, github=github,
                              resume_path=resume_path)


def _page_gone(exc: Exception) -> bool:
    """True when an error means the page/context/browser was closed or crashed —
    i.e. we can no longer act on it and must NOT restart the whole apply."""
    s = str(exc).lower()
    return any(x in s for x in ("has been closed", "target page", "target closed",
                                "page crashed", "browser has been closed",
                                "context or browser has been closed", "connection closed"))


async def _agent_apply(url: str, company: str, facts: dict, model: str, *, pk: str = "",
                       jd_text: str = "", resume_tex: str = "", github: str = "",
                       resume_path: str = "") -> dict:
    """Fallback: the browser-use agent loop (LLM-orchestrated) for portals the
    scripted pipeline can't handle. Same deterministic tools, same outcome shape."""
    try:
        from browser_use import Agent

        from tools.browser_llm import make_llm
    except Exception as exc:  # not installed
        log.error("browser-use not installed: %s (run ./setup.sh)", exc)
        return {"status": "unknown", "detail": "browser-use unavailable"}

    from urllib.parse import urlparse

    from core.config import get_settings

    facts = dict(facts)  # copy — we add auto-drafted answers across passes
    llm = make_llm(model)
    headed = not bool(getattr(get_settings(), "browser_headless", False))
    # Vision ON by default (Claude, GPT, Muse Spark, and other multimodal models).
    # Only known TEXT-ONLY models get it off — sending them screenshots 404s with
    # "no endpoints support image input" (e.g. Kimi K2).
    m = model.lower()
    text_only = any(x in m for x in ("kimi", "moonshotai/kimi", "qwen2.5-coder",
                                     "deepseek-chat", "deepseek-v3", "-instruct-text"))
    use_vision = not text_only
    # Upload from a clean, human-named copy ("<Name> Resume.pdf"): the raw artifact
    # name (contains '#') is ugly to a recruiter and can trip the upload widget.
    upload_path = _clean_resume_copy(resume_path, facts.get("Full name") or facts.get("Name") or "")
    files = [upload_path] if upload_path else None
    host = urlparse(url).hostname or ""
    allowed = list({host, f"*.{'.'.join(host.split('.')[-2:])}" if host else ""} - {""})
    # Deterministic helpers: upload_resume sets the file straight onto the page's
    # résumé input(s); verify_form_filled reads the ACTUAL field values from the
    # DOM right before submit (dynamic forms wipe fields on re-render, and the
    # model otherwise trusts its memory over the page).
    snapshot: list = []  # last verify_form_filled reading — what the form REALLY held
    drafted: dict[str, str] = {}  # writer answers from THIS run — persisted by caller
    controller = _apply_controller(
        upload_path, pk, url, snapshot_sink=snapshot, drafted_sink=drafted,
        company=company, jd_text=jd_text, resume_tex=resume_tex, github=github)
    shot = None

    def _done(res: dict) -> dict:
        # Attach the captured field map (every input + checkbox/radio choice) so
        # the dashboard can show exactly what was provided on the form, plus any
        # writer-drafted answers so the caller can bank them (never redraft).
        fields = _ui_fields(snapshot)
        if fields:
            res["fields"] = fields
        if drafted:
            res["drafted"] = drafted
        return res

    # A real, visible Chrome fenced to the ATS host — the fence makes wandering to
    # the company homepage physically impossible (Fable's #1 reliability lever).
    # keep_alive so the page survives agent.run() for the deterministic finalize.
    profile = _browser_profile(allowed, keep_alive=True)

    opts = {"llm": llm, "available_file_paths": files, "use_vision": use_vision,
            "max_actions_per_step": 2,  # multi-action batches hit a stale DOM
            "register_new_step_callback": _step_cb(pk, url),
            "extend_system_message": _APPLY_POLICY}
    if controller is not None:
        opts["controller"] = controller
    if profile is not None:
        opts["browser_profile"] = profile
    agent = Agent(task=_task(url, company, facts, upload_path), **opts)
    try:
        history = await agent.run(max_steps=_MAX_STEPS)
        text = (history.final_result() if hasattr(history, "final_result") else str(history)) or ""
        shot = _last_screenshot(history) or shot

        # Hard blockers browser-use reports (account wall) short-circuit.
        if "BLOCKED:" in text:
            q = text.split("BLOCKED:", 1)[1].strip()[:200]
            reason = "captcha" if "captcha" in q.lower() else "no_account"
            return _done({"status": "gate", "reason": reason, "screenshot_b64": shot,
                          "question": q})

        # browser-use FILLED the form (incl. the vision-picked dropdowns). Hand its
        # live page to the deterministic finalize for submit → self-heal → confirm →
        # CAPTCHA handoff — the reliable, code-driven finish.
        page = getattr(agent, "browser_session", None)
        page = getattr(page, "agent_current_page", None) if page else None
        if page is None:
            return _done({"status": "uncertain", "screenshot_b64": shot,
                          "detail": "browser-use filled the form but its page was unavailable"})
        heal_facts = {**facts, **drafted}
        result = await _finalize_submit(page, heal_facts, drafted, pk, url, headed=headed)
        if "fields" not in result and (uf := _ui_fields(snapshot)):
            result["fields"] = uf
        return result
    finally:
        try:
            await agent.browser_session.kill()
        except Exception:
            pass


def _task(url: str, company: str, facts: dict, resume_path: str) -> str:
    facts_str = "\n".join(f"- {q}: {a}" for q, a in facts.items() if a) or "(none provided)"
    resume_line = (
        "\nThis application REQUIRES a résumé/CV attachment. To attach it, call the "
        "`upload_resume` action — it places the résumé on the correct required field "
        "automatically (do NOT use the generic file upload for the résumé, and ignore "
        "any optional 'Autofill from resume' uploader). After calling it, confirm the "
        "résumé's filename shows in the résumé field before you continue.\n"
        if resume_path else ""
    )
    return (
        f"Go to {url} and complete AND SUBMIT the job application for {company}.\n"
        f"{resume_line}"
        f"Fill the form using ONLY these approved answers:\n{facts_str}\n\n"
        "You have DETERMINISTIC tools — USE THEM; do not click or type fields one by "
        "one, and never navigate away from this application page.\n"
        "WORKFLOW (aim for under 12 steps):\n"
        "1. Open the application form (click the 'Application' tab if present). Stay "
        "on this page — never click links to the company homepage or other jobs.\n"
        "2. upload_resume (if a résumé field exists).\n"
        "3. For EVERY open-ended question (textarea / 'tell us about…' / 'why…') with "
        "no approved answer above, call draft_essay_answer FIRST and use its text.\n"
        "4. ONE fill_fields call — fields_json covering every TEXT / dropdown / "
        "textarea field at once (essays verbatim from draft_essay_answer).\n"
        "5. ONE select_choices call — choices_json mapping every radio/checkbox "
        "QUESTION to the option to pick. Use this for ALL yes/no and choice "
        "questions; do NOT click radios individually.\n"
        "6. For any DROPDOWN / autocomplete field (e.g. Location) that fill_fields "
        "could not set, open it and click the matching option yourself (you can see "
        "it) — this is where you're better than the tools.\n"
        "7. verify_form_filled. If it lists empty REQUIRED fields, fix ONLY those, "
        "then verify again.\n"
        "8. Do NOT click Submit — a separate automated step submits and confirms. "
        "When every required field shows a value, finish with:  FILLED\n\n"
        "Rules:\n"
        "- You must NEVER compose, invent, or paraphrase answer text yourself — not "
        "for essays, not for any field. Every value you type comes VERBATIM from the "
        "approved answers above or from draft_essay_answer. If neither has it, STOP "
        "and finish your response with exactly:"
        "  MISSING: <the field's QUESTION or LABEL text — never its placeholder like "
        "'Start typing…'>\n"
        "- If the portal requires creating an account, or shows a CAPTCHA challenge "
        "you would have to solve, STOP and finish with:  BLOCKED: <reason>\n"
        "- BEFORE submitting, call verify_form_filled — dynamic forms silently wipe "
        "fields when they re-render, so never trust that a field you typed earlier "
        "still holds its value. If it reports an empty REQUIRED field, re-fill that "
        "field with its approved answer and verify again. Only submit when the check "
        "says all required fields have values.\n"
        "- Then SUBMIT the form.\n"
        "- After submitting, LOOK for a real confirmation: a 'thank you' / 'application "
        "received' message, or a confirmation number. ONLY if you actually SEE one, "
        "finish with:  APPLIED: <the confirmation text or number>\n"
        "- If you clicked submit but there is NO confirmation (the page redirected, "
        "went blank, or returned to a job-listings page), do NOT claim success — "
        "finish with:  UNCERTAIN: <what the page shows now>"
    )


# --- scripted apply: code drives, the model only maps + writes -----------------

_SUCCESS_RX = (r"thank you|application (has been |was )?submitted|submitted successfully"
               r"|received your application|we('|’)ve received")


async def _agent_finish_residuals(page, residual_map: dict, model: str, pk: str,  # noqa: ANN001
                                  url: str, *, company: str = "", jd_text: str = "",
                                  resume_tex: str = "", github: str = "") -> None:
    """Hand ONLY the fields the deterministic pipeline couldn't set (a stubborn
    Ashby Location autocomplete, an odd custom widget) to a SHORT, vision-enabled
    browser-use agent running on the SAME page. The bulk stays deterministic; the
    LLM touches just these residual fields, is capped tight, and never submits."""
    if not residual_map:
        return
    try:
        from browser_use import Agent

        from tools.browser_llm import make_llm
    except Exception as exc:
        log.warning("browser-use unavailable for residual finish: %s", exc)
        return
    m = model.lower()
    text_only = any(x in m for x in ("kimi", "moonshotai/kimi", "qwen2.5-coder",
                                     "deepseek-chat", "deepseek-v3", "-instruct-text"))
    lines = "\n".join(f"- {k!r} = {v!r}" for k, v in residual_map.items())
    task = (
        "The job-application form on THIS page is already almost entirely filled. "
        "Complete ONLY the fields listed below, then STOP. Do NOT click Submit or "
        "Apply — a separate step submits.\n" + lines + "\n\n"
        "Each is usually a dropdown or autocomplete: click the field, type the value, "
        "wait for the option list, and CLICK the suggestion that matches.\n"
        "IMPORTANT matching rule: a more-qualified suggestion is the SAME place and "
        "IS the right choice — for the value 'Seattle' you MUST select "
        "'Seattle, Washington, United States' (or 'Seattle, WA, USA'). Selecting it "
        "is required; leaving the field with just typed text is a FAILURE. The ONLY "
        "reason to skip a field is if NO suggestion contains your city at all — and "
        "never pick a DIFFERENT city (do not pick 'Settle' or another town for "
        "'Seattle'). When each field shows a selected option, finish with: DONE.")
    controller = _apply_controller("", pk, url, company=company, jd_text=jd_text,
                                   resume_tex=resume_tex, github=github)
    opts = {"llm": make_llm(model), "page": page, "use_vision": not text_only,
            "max_actions_per_step": 2, "register_new_step_callback": _step_cb(pk, url),
            "extend_system_message": _APPLY_POLICY}
    if controller is not None:
        opts["controller"] = controller
    _emit(pk, "response", agent="browser", url=url,
          detail=f"🤖 handing stubborn field(s) to browser-use: {list(residual_map)[:3]}")
    try:
        agent = Agent(task=task, **opts)
        await agent.run(max_steps=8)  # tight budget — residuals only, no wandering
    except Exception as exc:
        log.warning("residual agent finish failed: %s", exc)
    # Do NOT kill the session here — it wraps the scripted page we still submit on.


async def _scripted_apply(url: str, company: str, facts: dict, model: str, *,
                          pk: str = "", jd_text: str = "", resume_tex: str = "",
                          github: str = "", resume_path: str = "") -> dict | None:
    """Deterministic Playwright apply. Returns the outcome dict, or None when the
    page doesn't fit the script (no fillable form found) → caller falls back."""
    import asyncio
    import base64

    from playwright.async_api import async_playwright

    from core.config import get_settings
    from tools.narrative import draft_answer

    upload_path = _clean_resume_copy(
        resume_path, facts.get("Full name") or facts.get("Name") or "")
    drafted: dict[str, str] = {}

    async with async_playwright() as p:
        page, closer, headed = await _launch(p)

        async def _maybe_assist(res: dict) -> dict:
            """In a VISIBLE browser, hand a filled-but-blocked form to the human
            (CAPTCHA / a stubborn widget), wait for them to finish, and upgrade to
            a real 'applied' if they submit. Never touches the CAPTCHA itself."""
            if not (headed and get_settings().assist_captcha):
                return res
            if res.get("status") not in ("gate", "uncertain"):
                return res
            conf = await _assist_wait(page, pk, url, res.get("question", ""),
                                      res.get("reason", ""))
            if not conf:
                return res
            st = await page.evaluate(_READ_FORM_JS)
            return {"status": "applied", "confirmation": conf,
                    "screenshot_b64": base64.b64encode(await page.screenshot()).decode(),
                    "drafted": drafted or None, "fields": _ui_fields(st)}

        try:
            _emit(pk, "response", agent="browser", url=url, detail="▶ scripted apply: opening form")
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1500)

            # Reach the actual form: Ashby has an "Application" tab; Greenhouse and
            # Lever variants use an Apply link/button. All best-effort.
            for sel in ('role=tab[name*="Application"]', 'text="Application"',
                        'a:has-text("Apply for this job")', 'a:has-text("Apply now")',
                        'button:has-text("Apply")'):
                try:
                    await page.locator(sel).first.click(timeout=2500)
                    await page.wait_for_timeout(1200)
                    break
                except Exception:
                    continue

            fields = await page.evaluate(_READ_FORM_JS)
            fillable = [f for f in fields if f.get("type") not in ("submit", "button")]
            if len([f for f in fillable if f.get("type") not in ("file",)]) < 2:
                return None  # no real form here — let the agent loop try

            if any(f.get("type") == "password" for f in fillable) and not any(
                    "login" in k.lower() for k in facts):
                shot = base64.b64encode(await page.screenshot()).decode()
                return {"status": "gate", "reason": "no_account", "screenshot_b64": shot,
                        "question": "The portal wants a login before the application form."}

            if upload_path:
                n = await _set_resume_on_page(page, upload_path)
                _emit(pk, "response", agent="browser", url=url,
                      detail=f"📎 résumé attached to {n} field(s)")
                await page.wait_for_timeout(1200)

            # ONE mapping call: field labels -> fact KEYS (code substitutes values,
            # so the model cannot invent text) or ESSAY/SKIP markers.
            mapping = await asyncio.to_thread(
                _map_fields, fields, facts, company, jd_text)
            fill_map: dict[str, str] = {}
            missing_required: list[str] = []
            for f in fields:
                label = f.get("label", "")
                if not label or f.get("type") in ("file", "submit", "button"):
                    continue
                verdict = mapping.get(label) or mapping.get(label.strip()) or "SKIP"
                if not isinstance(verdict, str):  # model returned a nested object
                    verdict = "SKIP"
                if verdict == "ESSAY":
                    q = label
                    ans = facts.get(q) or ""
                    if not ans:
                        d = await asyncio.to_thread(draft_answer, q, company, jd_text,
                                                    resume=resume_tex, github=github)
                        ans = d.get("answer") or ""
                        if ans:
                            drafted[q] = ans
                            _emit(pk, "response", agent="writer", url=url,
                                  detail=f"drafted answer → {q[:70]}")
                    if ans:
                        fill_map[label] = ans
                    elif f.get("required"):
                        missing_required.append(label)
                elif verdict != "SKIP":
                    val = facts.get(verdict, "")
                    if val:
                        fill_map[label] = val
                    elif f.get("required") and f.get("type") not in ("checkbox",):
                        missing_required.append(label)
                elif f.get("required") and f.get("type") not in ("checkbox", "radio"):
                    missing_required.append(label)

            if missing_required:
                shot = base64.b64encode(await page.screenshot()).decode()
                return _with_fields(page, {
                    "status": "gate", "reason": "unknown_field", "screenshot_b64": shot,
                    "question": "I need answers for: " + "; ".join(missing_required[:5]),
                    "drafted": drafted or None})

            _emit(pk, "response", agent="browser", url=url,
                  detail=f"⚡ one-shot fill: {len(fill_map)} fields")
            await page.evaluate(_FILL_JS, fill_map)
            wanted = list(fill_map.values())
            for _ in range(4):  # commit autocomplete dropdowns (Location etc.)
                await page.wait_for_timeout(700)
                picked = await page.evaluate(_PICK_OPTION_JS, wanted)
                if not picked:
                    break
                _emit(pk, "response", agent="browser", url=url, detail=f"picked: {picked}")

            # Radio/checkbox GROUPS: React ignores synthetic clicks — select each
            # with a REAL Playwright click on the option element.
            choice_map = {f["label"]: fill_map[f["label"]] for f in fields
                          if f.get("type") == "choice-group" and f.get("label") in fill_map}
            if choice_map:
                await _set_choices(page, choice_map, pk, url)

            # Combobox widgets ignore synthetic value-setting entirely (Ashby's
            # Location). Drive each one with a REAL click → pick → type → pick.
            combos = {f["label"]: fill_map[f["label"]] for f in fields
                      if f.get("combo") and f.get("label") in fill_map}
            if combos:
                combos = {k: (v.split(",")[0].strip() if "location" in k.lower() else v)
                          for k, v in combos.items()}
                _emit(pk, "response", agent="browser", url=url,
                      detail=f"🎯 combobox pass: {list(combos)}")
                await _fix_fields(page, combos, pk, url)

            async def _still_empty(keys: list[str]) -> tuple[list[str], list]:
                st = await page.evaluate(_READ_FORM_JS)

                def _val(key: str, _st: list = st) -> str:
                    kl = key.lower()[:24]
                    f = next((f for f in _st
                              if kl in (f.get("label") or "").lower()
                              or (f.get("label") or "").lower()[:24] in key.lower()), None)
                    return (f or {}).get("value") or ""
                return [k for k in keys if not _val(k)], st

            # Verify against the DOM; refill anything the re-render wiped, once.
            state = await page.evaluate(_READ_FORM_JS)
            empty_req = [f["label"] for f in state if f.get("required") and not
                         f.get("value") and f.get("type") not in ("radio", "checkbox", "file")]
            refill = {k: v for k, v in fill_map.items() if k in empty_req}
            if refill:
                _emit(pk, "response", agent="browser", url=url,
                      detail=f"🔁 refill after re-render: {list(refill)[:4]}")
                await page.evaluate(_FILL_JS, refill)
                await page.wait_for_timeout(800)

            import re as _re

            _ERRORS_JS = """
              () => [...new Set([...document.querySelectorAll(
                       '[class*="error" i], [role="alert"], [aria-invalid="true"]')]
                .map(e => (e.getAttribute('aria-invalid') === 'true'
                           ? ((e.labels && e.labels[0] && e.labels[0].textContent) || e.name || '')
                             + ' is invalid/empty'
                           : (e.textContent || '')).replace(/\\s+/g, ' ').trim())
                .filter(t => t && t.length > 2 && t.length < 140))].slice(0, 6)
            """
            body, shot, errors = "", None, []
            for attempt in (0, 1):  # submit → read the form's errors → fix → resubmit
                clicked = await page.evaluate("""
                  () => {
                    const btns = [...document.querySelectorAll('button, input[type=submit]')];
                    const b = btns.find(x => /submit|apply/i.test(x.textContent || x.value || ''));
                    if (b) { b.click(); return (b.textContent || b.value || '').trim(); }
                    return '';
                  }""")
                if not clicked:
                    return None  # no submit control — not a scripted-friendly form
                _emit(pk, "response", agent="browser", url=url, detail=f"⏎ clicked: {clicked}")
                await page.wait_for_timeout(5000)

                body = (await page.evaluate("() => document.body.innerText || ''"))[:6000]
                shot = base64.b64encode(await page.screenshot()).decode()
                state = await page.evaluate(_READ_FORM_JS)
                m = _re.search(_SUCCESS_RX, body, _re.I)
                if m:
                    line = next((ln.strip() for ln in body.splitlines()
                                 if m.group(0).lower() in ln.lower()), m.group(0))
                    return {"status": "applied", "confirmation": line[:120],
                            "screenshot_b64": shot, "drafted": drafted or None,
                            "fields": _ui_fields(state)}
                errors = await page.evaluate(_ERRORS_JS)
                if not errors:
                    break  # nothing the form complains about — check CAPTCHA next
                if attempt == 1:
                    return await _maybe_assist({
                        "status": "gate", "reason": "unknown_field",
                        "screenshot_b64": shot, "drafted": drafted or None,
                        "fields": _ui_fields(state),
                        "question": "The form still rejects these — please set them in "
                                    "the open window: " + "; ".join(errors)})
                # SELF-HEAL: rebuild values for exactly the rejected fields — from the
                # fill map, else the best-matching fact — and retype them for real.
                fix_map: dict[str, str] = {}
                for err in errors:
                    hit = next(((k, v) for k, v in fill_map.items()
                                if k.lower() in err.lower()), None) or _fact_for(err, facts)
                    if hit:
                        k, v = hit
                        if "location" in err.lower():
                            v = v.split(",")[0].strip()  # autocompletes want the city
                        fix_map[k] = v
                if not fix_map:
                    return await _maybe_assist({
                        "status": "gate", "reason": "unknown_field",
                        "screenshot_b64": shot, "drafted": drafted or None,
                        "fields": _ui_fields(state),
                        "question": "Please set these in the open window: "
                                    + "; ".join(errors)})
                _emit(pk, "response", agent="browser", url=url,
                      detail=f"🔧 fixing rejected fields: {list(fix_map)[:4]}")
                await _fix_fields(page, fix_map, pk, url)
                await page.wait_for_timeout(800)
                # TRUTH CHECK: never resubmit (and risk a bogus CAPTCHA verdict)
                # while a healed field is verifiably still empty on the page.
                still_empty, state2 = await _still_empty(list(fix_map))
                if still_empty:
                    # The form rejected these and the deterministic filler couldn't
                    # set them (a stubborn Location autocomplete). Hand JUST these,
                    # with their values, to a short vision browser-use agent on THIS
                    # page — then re-check. Only a field it also can't set (value not
                    # offered) reaches the human.
                    residual = {k: fix_map[k] for k in still_empty if fix_map.get(k)}
                    if residual:
                        await _agent_finish_residuals(page, residual, model, pk, url,
                                                      company=company, jd_text=jd_text,
                                                      resume_tex=resume_tex, github=github)
                        await page.wait_for_timeout(600)
                        still_empty, state2 = await _still_empty(list(fix_map))
                if still_empty:
                    return await _maybe_assist({
                        "status": "gate", "reason": "unknown_field",
                        "screenshot_b64": base64.b64encode(await page.screenshot()).decode(),
                        "drafted": drafted or None, "fields": _ui_fields(state2),
                        "question": "These fields resist automation on this form — "
                                    "please set them manually when submitting: "
                                    + "; ".join(still_empty[:4])})

            # Diagnose the REAL blocker before ever blaming the CAPTCHA. Scan EVERY
            # required field (not just the ones we mapped) for an empty value — an
            # unfilled dropdown/choice that didn't commit is the usual culprit, and
            # the CAPTCHA is almost always present but NOT what's stopping the submit.
            state = await page.evaluate(_READ_FORM_JS)
            empty_req = [f for f in state if f.get("required") and not f.get("value")
                         and f.get("type") != "file"]  # file inputs read empty even when attached
            if empty_req:
                labels = [f["label"] for f in empty_req]
                stubborn = [f["label"] for f in empty_req
                            if f.get("combo") or f.get("type") == "choice-group"]
                q = "Some required fields didn't fill automatically"
                if stubborn:
                    q += (" — a dropdown/choice needs a manual pick: "
                          + "; ".join(stubborn[:3]))
                q += ". Set these and submit: " + "; ".join(labels[:5])
                return await _maybe_assist({
                    "status": "gate", "reason": "unknown_field",
                    "screenshot_b64": base64.b64encode(await page.screenshot()).decode(),
                    "drafted": drafted or None, "fields": _ui_fields(state),
                    "question": q})
            # Only NOW, with every required field truly filled, is the CAPTCHA the blocker.
            if _re.search(r"captcha", body, _re.I) or await page.locator(
                    'iframe[src*="recaptcha"], iframe[src*="hcaptcha"]').count():
                return await _maybe_assist({
                    "status": "gate", "reason": "captcha", "screenshot_b64": shot,
                    "drafted": drafted or None, "fields": _ui_fields(state),
                    "question": "All fields are filled — only the CAPTCHA remains. "
                                "Solve it in the open window and submit."})
            errs = [f["label"] for f in await page.evaluate(_READ_FORM_JS)
                    if f.get("required") and not f.get("value")
                    and f.get("type") not in ("radio", "checkbox", "file")]
            detail = (f"submit clicked but no confirmation; still-empty required: {errs[:4]}"
                      if errs else "submit clicked but no confirmation text appeared")
            return await _maybe_assist({
                "status": "uncertain", "detail": detail[:200], "screenshot_b64": shot,
                "drafted": drafted or None, "fields": _ui_fields(state)})
        finally:
            await closer()


def _browser_profile(allowed_domains: list, keep_alive: bool = False):  # noqa: ANN201
    """A browser-use BrowserProfile: real Chrome + persistent profile + FENCED to
    the ATS host (navigation off-domain is blocked, not just discouraged)."""
    try:
        from browser_use import BrowserProfile
    except Exception:
        try:
            from browser_use.browser import BrowserProfile
        except Exception:
            return None
    from pathlib import Path

    from core.config import get_settings

    s = get_settings()
    base = {"headless": bool(getattr(s, "browser_headless", False)),
            "keep_alive": keep_alive,
            "wait_between_actions": 0.8,
            "wait_for_network_idle_page_load_time": 1.0}
    if allowed_domains:
        base["allowed_domains"] = allowed_domains
    extras = {}
    if (ch := (getattr(s, "browser_channel", "") or "").strip()):
        extras["channel"] = ch
    if (prof := _profile_dir()):
        Path(prof).mkdir(parents=True, exist_ok=True)
        extras["user_data_dir"] = str(Path(prof).resolve())
    for kw in ({**base, **extras}, base):  # real-Chrome extras are best-effort
        try:
            return BrowserProfile(**kw)
        except Exception as exc:
            log.warning("BrowserProfile(%s) failed: %s", list(kw), exc)
    return None


async def _launch(p):  # noqa: ANN001, ANN201
    """Launch the apply browser. Prefers REAL Google Chrome + a persistent profile
    (real fingerprint + accumulated cookies → far fewer CAPTCHAs, and reusable
    portal logins); falls back to bundled Chromium. Returns (page, close, headed)."""
    from pathlib import Path

    from core.config import get_settings

    s = get_settings()
    headless = bool(getattr(s, "browser_headless", False))
    channel = (getattr(s, "browser_channel", "") or "").strip() or None
    profile = _profile_dir()
    args = ["--no-first-run", "--no-default-browser-check"]
    if profile:
        Path(profile).mkdir(parents=True, exist_ok=True)
        root = str(Path(profile).resolve())
        for ch in ([channel, None] if channel else [None]):
            try:
                ctx = await p.chromium.launch_persistent_context(
                    root, headless=headless, channel=ch, args=args)
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                log.info("apply browser: real profile (channel=%s, headless=%s)", ch, headless)
                return page, ctx.close, (not headless)
            except Exception as exc:
                log.warning("persistent context (channel=%s) failed: %s", ch, exc)
    for ch in ([channel, None] if channel else [None]):
        try:
            b = await p.chromium.launch(headless=headless, channel=ch, args=args)
            return await b.new_page(), b.close, (not headless)
        except Exception as exc:
            log.warning("launch (channel=%s) failed: %s", ch, exc)
    b = await p.chromium.launch(headless=headless)
    return await b.new_page(), b.close, (not headless)


async def _assist_wait(page, pk: str, url: str, note: str = "",  # noqa: ANN001
                       reason: str = "") -> str | None:
    """Human handoff: the form is filled and visible — wait for the person to finish
    whatever's actually blocking (a stubborn field, or a CAPTCHA if one is present)
    and click Submit, then return the real confirmation text. The message states the
    REAL blocker — it never claims a CAPTCHA when the blocker is a field."""
    import re as _re

    from core.config import get_settings

    secs = int(getattr(get_settings(), "assist_wait_seconds", 240))
    if reason == "captcha":
        ask = "solve the CAPTCHA and click Submit"
    elif note:
        ask = f"{note.rstrip('.')}, then click Submit"
    else:
        ask = "finish anything the form still needs and click Submit"
    _emit(pk, "gate", agent="applier", url=url,
          detail=f"🙋 Over to you: the application is filled in the open browser "
                 f"window — {ask}. Watching for confirmation for up to {secs // 60} min…")
    try:
        await page.bring_to_front()
    except Exception:
        pass
    for _ in range(max(1, secs // 3)):
        try:
            await page.wait_for_timeout(3000)
            body = (await page.evaluate("() => document.body.innerText || ''"))[:6000]
            url_now = page.url
        except Exception as exc:
            if _page_gone(exc):
                # You closed the window (often right AFTER submitting). We can't read
                # the page to confirm — stop waiting and let the caller keep the gate;
                # we NEVER resubmit on our own.
                _emit(pk, "response", agent="applier", url=url,
                      detail="⚠️ the browser window closed — I can't auto-confirm. "
                             "If you submitted it, mark it applied on the dashboard.")
                return None
            continue
        m = _re.search(_SUCCESS_RX, body, _re.I)
        # A redirect to a confirmation/success URL is also proof of submission.
        url_ok = url_now != url and bool(
            _re.search(r"confirmation|submitted|thank|success|application-complete", url_now, _re.I))
        if m or url_ok:
            line = (next((ln.strip() for ln in body.splitlines()
                          if m and m.group(0).lower() in ln.lower()), None)
                    or (m.group(0) if m else "Application submitted (confirmation page)."))
            _emit(pk, "response", agent="applier", url=url,
                  detail=f"✅ you submitted it — confirmed: {line[:80]}")
            return line[:120]
    return None


def _with_fields(page, res: dict) -> dict:  # noqa: ANN001 - small shaping helper
    res = {k: v for k, v in res.items() if v is not None}
    return res


def _fact_for(err: str, facts: dict) -> tuple[str, str] | None:
    """Best fact (key, value) for a validation-error message, by word overlap."""
    e = {w for w in err.lower().split() if len(w) > 3}
    best, score = None, 0
    for k, v in facts.items():
        if not v:
            continue
        n = len(e & {w for w in k.lower().split() if len(w) > 3})
        if n > score:
            best, score = (k, v), n
    return best


async def _fill_combobox(page, label: str, value: str, pk: str = "", url: str = "") -> bool:  # noqa: ANN001
    """Robustly fill an autocomplete/combobox (Ashby Location, Greenhouse city
    pickers) — the class of field synthetic value-setting can't touch. Locates the
    REAL input by its resolved label, clicks it, types with actual keystrokes, then
    polls for the async suggestion list and clicks the best match. Handles plain
    text inputs too (types the value, no pick). Returns True iff the field ended
    up with a value."""
    try:
        handle = await page.evaluate_handle(_LOCATE_INPUT_JS, label)
        el = handle.as_element()
        if el is None:
            return False
        try:
            await el.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            pass
        await el.click(timeout=2500)
        await page.wait_for_timeout(250)
        is_auto = await el.evaluate("""e => !!(e.getAttribute('role') === 'combobox'
            || e.getAttribute('aria-autocomplete') || e.getAttribute('aria-haspopup')
            || e.getAttribute('aria-controls') || e.closest('[role=combobox]'))""")
        # A city picker wants just the city token; a plain field wants the full value.
        typed = value.split(",")[0].strip() if (is_auto and "," in value) else value
        try:
            await el.fill("", timeout=1500)
        except Exception:
            pass
        # Strict dropdowns render every option on click — try a pick before typing.
        picked = await page.evaluate(_PICK_OPTION_JS, [value, typed])
        if picked:
            _emit(pk, "response", agent="browser", url=url, detail=f"📍 picked: {picked}")
            return True
        await el.type(typed, delay=40, timeout=6000)
        await page.wait_for_timeout(450)
        for _ in range(7):  # suggestions arrive async — poll, then click the match
            picked = await page.evaluate(_PICK_OPTION_JS, [value, typed])
            if picked:
                _emit(pk, "response", agent="browser", url=url, detail=f"📍 picked: {picked}")
                await page.wait_for_timeout(200)
                return True
            has_opts = await page.evaluate(
                "() => !!document.querySelector('[role=option], [role=listbox] li')")
            if not has_opts:
                break  # no dropdown at all → it's a plain text field
            await page.wait_for_timeout(400)
        val = (await el.evaluate("e => (e.value || '').trim()")) or ""
        if not is_auto and val:
            return True  # plain input: the typed value stuck
        if is_auto:
            # Keyboard-commit the first FILTERED suggestion (handles widgets whose
            # options aren't [role=option]) — but ACCEPT it only if what committed
            # actually matches the city. Never blind-commit a random first item
            # (that submits the wrong city, e.g. "Twelve Mile, Indiana").
            await el.press("ArrowDown")
            await page.wait_for_timeout(200)
            await el.press("Enter")
            await page.wait_for_timeout(250)
            got = ((await el.evaluate("e => (e.value || e.textContent || '').trim()")) or "").lower()
            city = value.split(",")[0].strip().lower()
            if got and (typed.lower() in got or city in got):
                _emit(pk, "response", agent="browser", url=url, detail=f"📍 selected: {got[:40]}")
                return True
            try:
                await el.fill("", timeout=1000)  # clear a wrong/stray commit → gate honestly
            except Exception:
                pass
            return False
        return bool(val)
    except Exception as exc:
        log.warning("combobox fill failed for %r: %s", label, exc)
        return False


async def _fix_fields(page, fix_map: dict, pk: str, url: str) -> None:  # noqa: ANN001
    """Targeted repair of fields the form rejected. Tries the robust combobox/real-
    keystroke filler first (handles autocompletes and plain inputs), then a
    get_by_label keystroke fallback, then a synthetic JS last resort."""
    for k, v in fix_map.items():
        if await _fill_combobox(page, k, v, pk, url):
            continue
        try:
            loc = page.get_by_label(k[:50], exact=False).first
            await loc.click(timeout=2000)
            await page.wait_for_timeout(600)
            picked = await page.evaluate(_PICK_OPTION_JS, [v])
            if picked:
                _emit(pk, "response", agent="browser", url=url, detail=f"picked: {picked}")
                continue
            await loc.fill("", timeout=2000)
            await loc.type(v, delay=25, timeout=6000)
            await page.wait_for_timeout(800)
            picked = await page.evaluate(_PICK_OPTION_JS, [v])
            if picked:
                _emit(pk, "response", agent="browser", url=url, detail=f"picked: {picked}")
            else:
                await loc.press("Tab")
        except Exception:  # radio groups / unlabeled controls — JS path
            try:
                await page.evaluate(_FILL_JS, {k: v})
                await page.wait_for_timeout(600)
                await page.evaluate(_PICK_OPTION_JS, [v])
            except Exception:
                pass


def _map_fields(fields: list, facts: dict, company: str, jd_text: str) -> dict:
    """ONE LLM call mapping each form label to a fact KEY, 'ESSAY', or 'SKIP'.
    Values are substituted in code, so the model can never invent an answer."""
    from litellm import completion

    from core.config import get_settings

    def _fline(f: dict) -> str:
        opts = f" (options: {' | '.join(f['options'][:8])})" if f.get("options") else ""
        req = " REQUIRED" if f.get("required") else ""
        return f"- {f.get('label')!r} [{f.get('type')}{opts}]{req}"

    flist = "\n".join(_fline(f) for f in fields
                      if f.get("label") and f.get("type") not in ("file", "submit", "button"))
    keys = "\n".join(f"- {k!r}" for k, v in facts.items() if v)
    prompt = (
        "Map each job-application form field to the candidate fact that answers it.\n\n"
        "FORM FIELDS — a [choice-group] is one QUESTION answered by picking one of "
        f"its options:\n{flist}\n\n"
        f"AVAILABLE FACT KEYS (the only permitted sources):\n{keys}\n\n"
        "Return ONLY a JSON object mapping EVERY field label to exactly one of:\n"
        "- a fact key (verbatim from the list) whose value answers it. For a "
        "choice-group, pick the fact whose value matches one of the options (e.g. a "
        "sponsorship question maps to the sponsorship fact whose value is 'Yes').\n"
        "- \"ESSAY\" for open-ended questions needing prose (tell us about…, why…, "
        "describe…).\n"
        "- \"SKIP\" for fields that are optional AND have no matching fact (EEO "
        "demographics with no fact value, marketing opt-ins), or don't apply.\n"
        "Never map a field to a fact that doesn't answer it."
    )
    model = get_settings().agent_model("") or "openai/gpt-4.1-mini"
    resp = completion(model=model, messages=[{"role": "user", "content": prompt}],
                      response_format={"type": "json_object"})
    try:
        out = json.loads(resp["choices"][0]["message"]["content"])
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


async def _page_of(browser_session) -> object:  # noqa: ANN001
    page = getattr(browser_session, "agent_current_page", None)
    if page is None and hasattr(browser_session, "get_current_page"):
        page = await browser_session.get_current_page()
    return page


_READ_FORM_JS = """
() => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const els = [...document.querySelectorAll('input, textarea, select')]
    .filter(el => el.type !== 'hidden');
  const optLabel = el => norm((el.labels && el.labels[0] && el.labels[0].textContent)
    || (el.closest('label') || {}).textContent || el.value || '');
  // The QUESTION a radio/checkbox group answers: fieldset legend, else the
  // nearest ancestor's text element that is NOT an option label (no input inside).
  const questionOf = el => {
    const fs = el.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg) return norm(lg.textContent); }
    let scope = el.parentElement;
    for (let i = 0; i < 6 && scope; i++) {
      const cand = [...scope.children]
        .filter(e => !e.querySelector('input,textarea,select') &&
                     !['INPUT','TEXTAREA','SELECT','SCRIPT','STYLE'].includes(e.tagName))
        .map(e => norm(e.textContent))
        .find(t => t.length > 12);
      if (cand) return cand;
      scope = scope.parentElement;
    }
    return '';
  };
  const out = [], groups = {};
  for (const el of els) {
    const multi = el.name &&
      document.querySelectorAll('input[name="' + CSS.escape(el.name) + '"]').length > 1;
    if (el.type === 'radio' || (el.type === 'checkbox' && multi)) {
      const key = el.name || optLabel(el);
      if (!groups[key]) groups[key] = {
        label: questionOf(el) || key, type: 'choice-group', options: [], value: '',
        required: !!(el.required || el.getAttribute('aria-required') === 'true') };
      const ol = optLabel(el);
      if (ol) groups[key].options.push(ol);
      if (el.checked) groups[key].value = ol || 'checked';
      if (el.required) groups[key].required = true;
      continue;
    }
    // Real label first; if only a placeholder ('Start typing…'), find the actual
    // question text next to the control — placeholders are not labels. Falls back
    // to the nearest preceding text (short unbound labels like 'Full name'), so
    // this MUST stay identical to _FILL_JS's labelOf or fill reports NOT FOUND.
    let lbl = (el.labels && el.labels[0] && norm(el.labels[0].textContent))
              || el.getAttribute('aria-label') || '';
    if (!lbl) lbl = questionOf(el);
    if (!lbl) {
      let sib = el.previousElementSibling;
      for (let i = 0; i < 3 && sib; i++) {
        const t = norm(sib.textContent);
        if (t && t.length > 1 && !sib.querySelector('input,textarea,select')) { lbl = t; break; }
        sib = sib.previousElementSibling;
      }
    }
    if (!lbl) {
      const pq = el.parentElement && [...el.parentElement.children]
         .filter(e => e !== el && !e.querySelector('input,textarea,select')
                      && !['INPUT','TEXTAREA','SELECT','SCRIPT','STYLE'].includes(e.tagName))
         .map(e => norm(e.textContent)).find(t => t.length > 1);
      if (pq) lbl = pq;
    }
    if (!lbl) lbl = el.placeholder || el.name || el.type || el.tagName.toLowerCase();
    const combo = !!(el.getAttribute('role') === 'combobox'
                     || el.getAttribute('aria-haspopup')
                     || el.getAttribute('aria-autocomplete')
                     || el.closest('[role="combobox"]')
                     || /start typing/i.test(el.placeholder || ''));
    out.push({
      label: lbl,
      combo,
      type: el.type || el.tagName.toLowerCase(),
      value: el.type === 'checkbox' ? (el.checked ? 'checked' : '')
           : (el.type === 'file' ? (el.files && el.files.length ? el.files[0].name : '')
                                 : (el.value || '')),
      required: !!(el.required || el.getAttribute('aria-required') === 'true'),
    });
  }
  out.push(...Object.values(groups));
  return out;
}
"""


def _ui_fields(snapshot: list) -> list[dict]:
    """Map the raw DOM reading to the dashboard's field-map shape — every text
    value that was provided plus EVERY checkbox/radio with its choice."""
    out = []
    for f in snapshot or []:
        label, ftype, val = f.get("label", ""), f.get("type", "text"), f.get("value", "")
        if ftype == "radio":
            if val != "checked":
                continue  # show only the selected option of each radio group
            out.append({"label": label, "type": "checkbox", "value": True})
        elif ftype == "checkbox":
            out.append({"label": label, "type": "checkbox", "value": val == "checked"})
        elif val:  # text/select/file — only what was actually provided
            out.append({"label": label, "type": ftype, "value": val})
    return out


_FILL_JS = """
(map) => {
  const norm = s => (s || '').toLowerCase().replace(/\\s+/g, ' ').trim();
  const ntrim = s => (s || '').replace(/\\s+/g, ' ').trim();
  const setVal = (el, v) => {
    const proto = el.tagName === 'TEXTAREA'
      ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const ctrls = [...document.querySelectorAll('input, textarea, select')]
    .filter(e => e.type !== 'hidden');
  const isText = el => !['radio','checkbox','file','submit','button','hidden'].includes(el.type);
  // The SAME label resolver READ_FORM uses, so a label the agent read back
  // matches here — climb to the question text when there's no bound <label>
  // (Ashby/Greenhouse rarely associate labels, which used to cause NOT FOUND).
  const questionOf = el => {
    const fs = el.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg) return ntrim(lg.textContent); }
    let scope = el.parentElement;
    for (let i = 0; i < 6 && scope; i++) {
      const cand = [...scope.children]
        .filter(e => !e.querySelector('input,textarea,select') &&
                     !['INPUT','TEXTAREA','SELECT','SCRIPT','STYLE'].includes(e.tagName))
        .map(e => ntrim(e.textContent))
        .find(t => t.length > 12);
      if (cand) return cand;
      scope = scope.parentElement;
    }
    return '';
  };
  const labelOf = el => {
    let lbl = (el.labels && el.labels[0] && ntrim(el.labels[0].textContent))
              || el.getAttribute('aria-label') || '';
    if (lbl) return lbl;
    lbl = questionOf(el);
    if (lbl) return lbl;
    let sib = el.previousElementSibling;  // short unbound label ("Full name", "Email")
    for (let i = 0; i < 3 && sib; i++) {
      const t = ntrim(sib.textContent);
      if (t && t.length > 1 && !sib.querySelector('input,textarea,select')) return t;
      sib = sib.previousElementSibling;
    }
    const pq = el.parentElement && [...el.parentElement.children]
       .filter(e => e !== el && !e.querySelector('input,textarea,select')
                    && !['INPUT','TEXTAREA','SELECT','SCRIPT','STYLE'].includes(e.tagName))
       .map(e => ntrim(e.textContent)).find(t => t.length > 1);
    if (pq) return pq;
    return el.placeholder || el.name || el.type || el.tagName.toLowerCase();
  };
  const STOP = new Set(['the','your','you','please','a','an','of','to','for','and','or',
    'is','in','on','url','no','this','that','role','enter','type','select','question',
    'optional','required','field','answer','what','which','with','are','will','do','does']);
  const toks = s => norm(s).split(' ').filter(t => t.length > 2 && !STOP.has(t));
  const report = [];
  for (const [key, val] of Object.entries(map)) {
    const k = norm(key);
    if (!k || !val) { report.push('SKIPPED (empty): ' + key); continue; }
    let done = false;
    for (const el of ctrls) {  // 1) text / textarea / select, matched by label
      if (!isText(el)) continue;
      const l = norm(labelOf(el));
      if (l && (l.includes(k) || k.includes(l))) {
        if (el.tagName === 'SELECT') {
          const opt = [...el.options].find(o => norm(o.textContent).includes(norm(val)));
          if (opt) { el.value = opt.value;
                     el.dispatchEvent(new Event('change', { bubbles: true })); done = true; }
        } else { setVal(el, val); done = true; }
        break;
      }
    }
    if (!done && k.length > 6) {  // 2) radio/checkbox: question text -> option label
      const qEl = [...document.querySelectorAll('label, legend, p, span, div')]
        .find(e => e.children.length < 6 && norm(e.textContent).includes(k));
      let scope = qEl && (qEl.closest('fieldset') || qEl.parentElement);
      for (let i = 0; i < 4 && scope; i++) {
        const boxes = [...scope.querySelectorAll('input[type=radio], input[type=checkbox]')];
        if (boxes.length) {
          const m = boxes.find(b => {
            const lbl = (b.labels && b.labels[0] && b.labels[0].textContent)
              || (b.closest('label') || {}).textContent || '';
            return norm(lbl).startsWith(norm(val)) || norm(lbl).includes(norm(val));
          });
          if (m) { m.click(); done = true; }
          else if (boxes.length === 1 &&
                   /^(yes|true|checked|agree|accept)/.test(norm(val))) {
            boxes[0].click(); done = true;  // lone consent checkbox
          }
          break;
        }
        scope = scope.parentElement;
      }
    }
    if (!done) {  // 3) fuzzy: reworded label -> EMPTY text field with the most
      const kt = toks(key);  //   shared significant tokens (clear unique winner)
      if (kt.length) {
        let best = null, bestScore = 0, second = 0;
        for (const el of ctrls) {
          if (!isText(el) || el.tagName === 'SELECT' || ntrim(el.value)) continue;
          const lt = new Set(toks(labelOf(el)));
          if (!lt.size) continue;
          const overlap = kt.filter(t => lt.has(t)).length / kt.length;
          if (overlap > bestScore) { second = bestScore; bestScore = overlap; best = el; }
          else if (overlap > second) { second = overlap; }
        }
        if (best && bestScore >= 0.5 && bestScore > second) { setVal(best, val); done = true; }
      }
    }
    report.push((done ? 'FILLED: ' : 'NOT FOUND: ') + key);
  }
  return report;
}
"""

_LOCATE_CHOICE_JS = """
({q, v}) => {
  const norm = s => (s || '').toLowerCase().replace(/\\s+/g, ' ').trim();
  const nq = norm(q), nv = norm(v);
  const qnode = [...document.querySelectorAll('legend,label,p,span,div,h3,h4')]
    .find(e => e.querySelectorAll('input').length === 0 && norm(e.textContent).includes(nq));
  let scope = qnode ? (qnode.closest('fieldset') || qnode.parentElement) : document.body;
  for (let i = 0; i < 6 && scope; i++) {
    const inputs = [...scope.querySelectorAll('input[type=radio], input[type=checkbox]')];
    if (inputs.length) {
      for (const inp of inputs) {
        const lbl = (inp.labels && inp.labels[0] && inp.labels[0].textContent)
          || (inp.closest('label') || {}).textContent || inp.value || '';
        if (norm(lbl) === nv || norm(lbl).startsWith(nv)) {
          return (inp.labels && inp.labels[0]) || inp.closest('label') || inp;
        }
      }
      return inputs[0];  // group found but no text match — first option as last resort
    }
    scope = scope.parentElement;
  }
  return null;
}
"""


_SUBMIT_JS = """
() => {
  const b = [...document.querySelectorAll('button, input[type=submit]')]
    .find(x => /submit|apply/i.test((x.textContent || x.value || '')));
  if (b) { b.click(); return (b.textContent || b.value || '').trim(); }
  return '';
}
"""

_ERRORS_JS = """
() => [...new Set([...document.querySelectorAll(
         '[class*="error" i], [role="alert"], [aria-invalid="true"]')]
  .map(e => (e.getAttribute('aria-invalid') === 'true'
             ? ((e.labels && e.labels[0] && e.labels[0].textContent) || e.name || '') + ' is empty'
             : (e.textContent || '')).replace(/\\s+/g, ' ').trim())
  .filter(t => t && t.length > 2 && t.length < 140))].slice(0, 6)
"""


async def _finalize_submit(page, facts: dict, drafted: dict, pk: str, url: str,  # noqa: ANN001
                           headed: bool) -> dict:
    """Deterministic finish on an already-FILLED page (browser-use did the filling,
    including the vision-picked dropdowns): submit → read the form's own errors →
    self-heal only those → resubmit; on real confirmation return applied; on a
    CAPTCHA / stubborn field, hand the visible window to the human and watch for
    the confirmation. Never touches a CAPTCHA itself."""
    import base64
    import re as _re

    from core.config import get_settings

    body, shot = "", None
    for attempt in (0, 1, 2):
        clicked = await page.evaluate(_SUBMIT_JS)
        if clicked:
            _emit(pk, "response", agent="browser", url=url, detail=f"⏎ clicked: {clicked}")
        await page.wait_for_timeout(5000)
        body = (await page.evaluate("() => document.body.innerText || ''"))[:6000]
        shot = base64.b64encode(await page.screenshot()).decode()
        state = await page.evaluate(_READ_FORM_JS)
        m = _re.search(_SUCCESS_RX, body, _re.I)
        if m:
            line = next((ln.strip() for ln in body.splitlines()
                         if m.group(0).lower() in ln.lower()), m.group(0))
            return {"status": "applied", "confirmation": line[:120], "screenshot_b64": shot,
                    "drafted": drafted or None, "fields": _ui_fields(state)}
        errors = await page.evaluate(_ERRORS_JS)
        if not errors or attempt >= 2:
            break
        # Self-heal: refill only the rejected fields from the facts (text + choices).
        text_fix, choice_fix = {}, {}
        for err in errors:
            hit = _fact_for(err, facts)
            if not hit:
                continue
            k, v = hit
            f = next((x for x in state if k.lower()[:20] in (x.get("label") or "").lower()), None)
            if f and f.get("type") == "choice-group":
                choice_fix[f["label"]] = v
            else:
                text_fix[k] = v.split(",")[0].strip() if "location" in err.lower() else v
        _emit(pk, "response", agent="browser", url=url,
              detail=f"🔧 self-heal rejected fields: {list(text_fix) + list(choice_fix)}")
        if text_fix:
            await page.evaluate(_FILL_JS, text_fix)
        if choice_fix:
            await _set_choices(page, choice_fix, pk, url)
        await page.wait_for_timeout(900)

    # Terminal: name the real blocker (empty required field, or a genuine CAPTCHA),
    # and in a visible window hand off to the human to finish + submit.
    state = await page.evaluate(_READ_FORM_JS)
    empty_req = [f for f in state if f.get("required") and not f.get("value")
                 and f.get("type") != "file"]
    captcha = bool(_re.search(r"captcha", body, _re.I)) or bool(await page.locator(
        'iframe[src*="recaptcha"], iframe[src*="hcaptcha"]').count())
    if headed and get_settings().assist_captcha:
        note = ("set the " + ", ".join(f["label"] for f in empty_req[:3])
                + (" field" if len(empty_req) == 1 else " fields")) if empty_req else ""
        conf = await _assist_wait(page, pk, url, note, "captcha" if captcha else "")
        if conf:
            st = await page.evaluate(_READ_FORM_JS)
            return {"status": "applied", "confirmation": conf,
                    "screenshot_b64": base64.b64encode(await page.screenshot()).decode(),
                    "drafted": drafted or None, "fields": _ui_fields(st)}
    if empty_req:
        labels = [f["label"] for f in empty_req]
        return {"status": "gate", "reason": "unknown_field", "screenshot_b64": shot,
                "drafted": drafted or None, "fields": _ui_fields(state),
                "question": "Set these and submit in the window: " + "; ".join(labels[:5])}
    if captcha:
        return {"status": "gate", "reason": "captcha", "screenshot_b64": shot,
                "drafted": drafted or None, "fields": _ui_fields(state),
                "question": "All fields filled — only the CAPTCHA remains. Solve it and submit."}
    return {"status": "uncertain", "detail": "submit clicked; no confirmation appeared",
            "screenshot_b64": shot, "drafted": drafted or None, "fields": _ui_fields(state)}


async def _set_choices(page, choices: dict, pk: str = "", url: str = "") -> list:  # noqa: ANN001
    """Select radio/checkbox options with REAL Playwright clicks on the option
    element — the only thing React-based groups (Ashby) reliably accept. `choices`
    maps question text -> option to pick. Returns the questions it clicked."""
    done = []
    for q, v in choices.items():
        try:
            handle = await page.evaluate_handle(_LOCATE_CHOICE_JS, {"q": q, "v": v})
            el = handle.as_element() if handle else None
            if not el:
                continue
            await el.scroll_into_view_if_needed(timeout=2000)
            await el.click(timeout=3000)
            await page.wait_for_timeout(250)
            done.append(q)
        except Exception:
            continue
    if done and pk:
        _emit(pk, "response", agent="browser", url=url,
              detail=f"◉ selected {len(done)} choice question(s)")
    return done


_PICK_OPTION_JS = """
(wanted) => {
  const norm = s => (s || '').toLowerCase().replace(/\\s+/g, ' ').trim();
  const vals = (wanted || []).map(norm).filter(Boolean);
  if (!vals.length) return '';
  // Never click a form-action control — only a suggestion whose text matches a
  // value we're setting (either direction: 'Seattle' ⊂ 'Seattle, WA, USA').
  const ACTION = /^(submit|apply|continue|next|back|previous|cancel|close|clear|search|save|done|remove|upload|browse|add|sign|log)\\b/;
  const matches = t => t && t.length < 80 && !ACTION.test(t)
                       && vals.some(v => v.includes(t) || t.includes(v));
  // Prefer real listbox/menu options; then fall back to ANY visible clickable a
  // combobox popup might render suggestions as — Ashby uses <button> elements, so
  // '[role=option]/li'-only found nothing and Location never got picked.
  const structured = [...document.querySelectorAll(
    '[role="option"], [role="listbox"] li, [role="listbox"] button, ' +
    '[role="menu"] [role="menuitem"], [class*="option" i], [class*="suggestion" i], ' +
    '[class*="autocomplete" i] li, [class*="result" i] li, [class*="menu" i] li')];
  let hit = structured.find(o => matches(norm(o.textContent)));
  if (!hit) {
    const clickable = [...document.querySelectorAll(
      'button, li, a, [role="button"], [role="option"]')]
      .filter(o => o.offsetParent !== null);  // visible only
    hit = clickable.find(o => matches(norm(o.textContent)));
  }
  if (hit) { hit.click(); return hit.textContent.trim().slice(0, 60); }
  return '';
}
"""


# Locate the ACTUAL <input> for a field label and return a handle to it — using
# the SAME label resolver as read/fill (so a label the mapper produced still
# points at the right control even when Ashby binds no <label>). get_by_label
# can't do this: the accessible name ("Location") rarely equals the read label
# ("Location (city, state, country)"), so it silently matches nothing.
_LOCATE_INPUT_JS = """
(label) => {
  const norm = s => (s || '').toLowerCase().replace(/\\s+/g, ' ').trim();
  const ntrim = s => (s || '').replace(/\\s+/g, ' ').trim();
  const questionOf = el => {
    const fs = el.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg) return ntrim(lg.textContent); }
    let scope = el.parentElement;
    for (let i = 0; i < 6 && scope; i++) {
      const cand = [...scope.children]
        .filter(e => !e.querySelector('input,textarea,select') &&
                     !['INPUT','TEXTAREA','SELECT','SCRIPT','STYLE'].includes(e.tagName))
        .map(e => ntrim(e.textContent))
        .find(t => t.length > 12);
      if (cand) return cand;
      scope = scope.parentElement;
    }
    return '';
  };
  const labelOf = el => {
    let lbl = (el.labels && el.labels[0] && ntrim(el.labels[0].textContent))
              || el.getAttribute('aria-label') || '';
    if (lbl) return lbl;
    lbl = questionOf(el);
    if (lbl) return lbl;
    let sib = el.previousElementSibling;
    for (let i = 0; i < 3 && sib; i++) {
      const t = ntrim(sib.textContent);
      if (t && t.length > 1 && !sib.querySelector('input,textarea,select')) return t;
      sib = sib.previousElementSibling;
    }
    const pq = el.parentElement && [...el.parentElement.children]
       .filter(e => e !== el && !e.querySelector('input,textarea,select')
                    && !['INPUT','TEXTAREA','SELECT','SCRIPT','STYLE'].includes(e.tagName))
       .map(e => ntrim(e.textContent)).find(t => t.length > 1);
    if (pq) return pq;
    return el.placeholder || el.name || el.type || el.tagName.toLowerCase();
  };
  const STOP = new Set(['the','your','you','please','a','an','of','to','for','and','or',
    'is','in','on','url','no','this','that','role','enter','type','select','question',
    'optional','required','field','answer','what','which','with','are','will','do','does',
    'city','state','country']);
  const toks = s => norm(s).split(' ').filter(t => t.length > 2 && !STOP.has(t));
  const k = norm(label);
  const ctrls = [...document.querySelectorAll('input, textarea, [role=combobox]')]
    .filter(e => !['hidden','file','submit','button','radio','checkbox'].includes(e.type || ''));
  let el = ctrls.find(e => { const l = norm(labelOf(e)); return l && (l.includes(k) || k.includes(l)); });
  if (!el) {
    const kt = toks(label);
    let best = null, bs = 0, sec = 0;
    for (const e of ctrls) {
      const lt = new Set(toks(labelOf(e)));
      if (!lt.size || !kt.length) continue;
      const ov = kt.filter(t => lt.has(t)).length / kt.length;
      if (ov > bs) { sec = bs; bs = ov; best = e; } else if (ov > sec) { sec = ov; }
    }
    if (best && bs >= 0.5 && bs > sec) el = best;
  }
  if (el) { try { el.scrollIntoView({block:'center'}); } catch(e){} }
  return el || null;
}
"""


def _apply_controller(resume_path: str, pk: str = "", url: str = "",
                      snapshot_sink: list | None = None,
                      drafted_sink: dict | None = None, company: str = "",
                      jd_text: str = "", resume_tex: str = "", github: str = ""):
    """Deterministic Playwright-backed actions for the apply agent:

    - upload_resume: sets the résumé PDF directly on the page's résumé file
      input(s) — never lands on the wrong (autofill) uploader.
    - verify_form_filled: reads the ACTUAL current value of every field from the
      DOM. Dynamic forms (Ashby) wipe fields when they re-render, and the model
      otherwise trusts its own memory — this is the ground truth before submit.
    """
    from browser_use import ActionResult, Controller

    # Remove built-ins that cause wandering / detours, so the model must use our
    # deterministic tools instead (Fable: force the custom tools).
    try:
        controller = Controller(exclude_actions=[
            "search_google", "switch_tab", "close_tab", "open_tab",
            "write_file", "read_file", "replace_file_str", "extract_structured_data"])
    except Exception:
        controller = Controller()

    @controller.registry.action(
        "Get the candidate's answer for an OPEN ENDED question (essay, 'why this "
        "role', 'tell us about…', 'share an example…'). A dedicated writer drafts it "
        "from the candidate's REAL professional record. You must NEVER compose essay "
        "text yourself — call this, then use its returned text VERBATIM in "
        "fill_fields. If it returns CANNOT_ANSWER, report MISSING for that question.")
    async def draft_essay_answer(question: str) -> ActionResult:
        import asyncio

        from tools.narrative import draft_answer as _draft

        d = await asyncio.to_thread(_draft, question, company, jd_text,
                                    resume=resume_tex, github=github)
        answer = d.get("answer") or ""
        if not answer:
            return ActionResult(
                extracted_content=f"CANNOT_ANSWER — report:  MISSING: {question}",
                include_in_memory=True)
        if drafted_sink is not None:
            drafted_sink[question] = answer  # banked by the caller — never redraft
        _emit(pk, "response", agent="writer", url=url,
              detail=f"drafted answer → {question[:70]}")
        return ActionResult(extracted_content=answer, include_in_memory=True)

    @controller.registry.action(
        "Fill MANY form fields in ONE shot. Pass fields_json: a JSON object ENCODED "
        "AS A STRING, mapping each visible field's label (or question text) to its "
        "answer — text inputs, textareas, dropdowns, AND radio/checkbox questions "
        "(map the question text to the option to pick, e.g. "
        '\'{"Name": "…", "Are you authorized to work…": "Yes"}\'). Fields are '
        "matched by label at execution time (index shifts can't break it), values are "
        "set the way the page's framework expects, and autocomplete dropdowns "
        "(placeholder 'Start typing…') are handled. Use THIS instead of typing field "
        "by field.")
    async def fill_fields(fields_json: str, browser_session) -> ActionResult:  # noqa: ANN001
        try:
            mapping = json.loads(fields_json)
            if not isinstance(mapping, dict):
                raise ValueError("fields_json must encode a JSON object")
        except Exception as exc:
            return ActionResult(extracted_content=f"fill_fields: bad fields_json ({exc}) — "
                                                  "pass a JSON object encoded as a string",
                                include_in_memory=True)
        try:
            page = await _page_of(browser_session)
            report = await page.evaluate(_FILL_JS, mapping)  # type: ignore[union-attr]
            # Autocomplete comboboxes (e.g. Location) open an option list after
            # typing — commit the option MATCHING our value so the right one sticks.
            wanted = [v for v in mapping.values() if isinstance(v, str)]
            for _ in range(3):
                await page.wait_for_timeout(700)  # type: ignore[union-attr]
                picked = await page.evaluate(_PICK_OPTION_JS, wanted)  # type: ignore[union-attr]
                if not picked:
                    break
                report.append(f"PICKED OPTION: {picked}")
        except Exception as exc:
            return ActionResult(extracted_content=f"fill_fields errored: {exc}",
                                include_in_memory=True)
        summary = "; ".join(report)[:600]
        _emit(pk, "response", agent="browser", url=url,
              detail=f"⚡ one-shot fill → {summary}"[:240])
        return ActionResult(extracted_content=f"fill_fields result: {summary}",
                            include_in_memory=True)

    @controller.registry.action(
        "Select radio-button / checkbox answers. Pass choices_json: a JSON object "
        "ENCODED AS A STRING mapping each question's text to the option to pick, e.g. "
        '\'{"Are you authorized to work…": "Yes", "Do you require sponsorship…": "Yes"}\'. '
        "Uses REAL clicks that React forms accept (Ashby). Use THIS for every "
        "radio/checkbox question — do not click them individually.")
    async def select_choices(choices_json: str, browser_session) -> ActionResult:  # noqa: ANN001
        try:
            mapping = json.loads(choices_json)
            if not isinstance(mapping, dict):
                raise ValueError("choices_json must encode a JSON object")
        except Exception as exc:
            return ActionResult(extracted_content=f"select_choices: bad json ({exc})",
                                include_in_memory=True)
        page = await _page_of(browser_session)
        done = await _set_choices(page, mapping, pk, url)
        return ActionResult(
            extracted_content=f"selected {len(done)}/{len(mapping)} choice question(s): "
                              + ", ".join(done)[:200], include_in_memory=True)

    @controller.registry.action(
        "Verify the form's REAL state: returns every visible field with its actual "
        "current value straight from the page (dynamic forms silently wipe fields "
        "on re-render — never trust memory). Call this RIGHT BEFORE submitting and "
        "re-fill any required field that is actually empty.")
    async def verify_form_filled(browser_session) -> ActionResult:  # noqa: ANN001
        try:
            page = await _page_of(browser_session)
            fields = await page.evaluate(_READ_FORM_JS)  # type: ignore[union-attr]
        except Exception as exc:
            return ActionResult(extracted_content=f"verify errored: {exc}",
                                include_in_memory=True)
        if snapshot_sink is not None:  # keep the latest reading for the dashboard
            snapshot_sink[:] = fields
        lines = [f"- {f['label']} [{f['type']}]{' REQUIRED' if f['required'] else ''}: "
                 f"{f['value'] or '(EMPTY)'}" for f in fields]
        empty_req = [f["label"] for f in fields
                     if f["required"] and not f["value"] and f["type"] not in ("radio",)]
        head = ("ALL required fields have values." if not empty_req else
                f"EMPTY REQUIRED FIELDS — fill these before submitting: {', '.join(empty_req)}")
        _emit(pk, "response", agent="browser", url=url,
              detail=f"🔍 pre-submit check: {head}"[:240])
        return ActionResult(extracted_content=head + "\n" + "\n".join(lines[:40]),
                            include_in_memory=True)

    if not resume_path:
        return controller

    @controller.registry.action(
        "Attach the candidate's résumé PDF to the résumé/CV upload field. Use THIS "
        "for the résumé (not the generic file upload); it targets the correct required "
        "field automatically. Call it once when a résumé upload is required.")
    async def upload_resume(browser_session) -> ActionResult:  # noqa: ANN001
        try:
            page = await _page_of(browser_session)
            n = await _set_resume_on_page(page, resume_path)
        except Exception as exc:
            log.warning("deterministic résumé upload errored: %s", exc)
            _emit(pk, "response", agent="browser", url=url,
                  detail=f"résumé upload ERRORED: {exc}")
            return ActionResult(extracted_content=f"résumé upload errored: {exc}",
                                include_in_memory=True)
        msg = (f"Attached the résumé PDF to {n} résumé field(s) on the page." if n
               else "No résumé file input was found on this page.")
        log.info("upload_resume: %s", msg)
        _emit(pk, "response", agent="browser", url=url, detail=f"📎 {msg}")
        return ActionResult(extracted_content=msg, include_in_memory=True)

    return controller


async def _set_resume_on_page(page: object, resume_path: str) -> int:
    """Set the résumé on every document-accepting file input on the page (covers the
    required résumé field; an optional autofill uploader parsing it is harmless).
    Returns how many inputs were set."""
    if page is None:
        return 0
    inputs = await page.query_selector_all('input[type="file"]')  # type: ignore[attr-defined]
    doc_inputs = []
    for inp in inputs:
        try:
            accept = ((await inp.get_attribute("accept")) or "").lower()
        except Exception:
            accept = ""
        if (not accept) or any(k in accept for k in ("pdf", "application", "word", ".doc")):
            doc_inputs.append(inp)
    n = 0
    for inp in (doc_inputs or inputs):
        try:
            await inp.set_input_files(resume_path)
            n += 1
        except Exception:
            pass
    return n


def _clean_resume_copy(src: str, name: str) -> str:
    """Copy the tailored PDF to a temp file named '<Name> Resume.pdf' — a clean
    name for the recruiter, and one that won't trip the upload widget (the raw
    artifact name contains '#'). Falls back to the original path on any error."""
    if not src:
        return ""
    import shutil
    import tempfile
    from pathlib import Path

    try:
        safe = "".join(c for c in name if c.isalnum() or c in " _").strip() or "Resume"
        d = Path(tempfile.gettempdir()) / "appliedin_uploads"
        d.mkdir(parents=True, exist_ok=True)
        dst = d / f"{safe} Resume.pdf"
        shutil.copyfile(src, dst)
        return str(dst)
    except Exception:
        return src


def _step_cb(pk: str, url: str):
    def _cb(_state: object, output: object, n: int) -> None:  # streamed to the UI
        goal = (getattr(output, "next_goal", "") or "").strip()
        if goal and pk:
            _emit(pk, "response", agent="browser", detail=f"[step {n}] {goal}"[:240], url=url)
    return _cb


def _emit(pk: str, kind: str, **fields: object) -> None:
    if not pk:
        return
    from core.events import emit
    emit(kind, pk=pk, **fields)


def _last_screenshot(history: object) -> str | None:
    """The final page screenshot as base64 PNG, so the UI can show what the agent
    saw when it finished (applied, or stuck at a gate)."""
    try:
        shots = history.screenshots(n_last=1) or []  # type: ignore[attr-defined]
    except Exception:
        return None
    return next((s for s in shots if s), None)
