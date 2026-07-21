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
_TOK_STOP = {"the", "your", "you", "please", "a", "an", "of", "to", "for", "and", "or",
             "is", "in", "on", "url", "no", "this", "that", "role", "enter", "type",
             "select", "question", "optional", "required", "field", "answer", "what",
             "which", "with", "are", "will", "do", "does", "all", "apply", "any"}


def _toks(s: str) -> set:
    """Significant lowercase word-tokens of a label (for fuzzy key↔label matching)."""
    words = "".join(c if c.isalnum() else " " for c in (s or "").lower()).split()
    return {t for t in words if len(t) > 2 and t not in _TOK_STOP}


def _is_text_only(model: str) -> bool:
    """True for models with NO vision — we send them no screenshots (image input
    404s them). The Kimi K2 line is text-only; Kimi K2.5+/K3 are multimodal, so
    they keep vision ON (better for dropdown/CAPTCHA-aware form filling)."""
    m = (model or "").lower()
    if "kimi-k2" in m and not any(v in m for v in ("k2.5", "k2.6", "k2.7")):
        return True
    return any(x in m for x in ("qwen2.5-coder", "deepseek-chat", "deepseek-v3",
                                "-instruct-text", "kimi-dev", "moonlight"))


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
    text_only = _is_text_only(model)
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
        await _click_fields_pass(page, pk, url)  # last fill step: click-commit every box/text field
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
        "- If a sign-in wall says it EMAILED a verification code to the candidate, "
        "call fetch_email_code and type the code it returns (NO_CODE → wait ~20s, "
        "retry once). Only a code sent by SMS / an authenticator app / a trusted "
        "device is a hard blocker.\n"
        "- If the portal requires creating an account, shows a CAPTCHA challenge "
        "you would have to solve, or wants a verification code you cannot fetch "
        "(SMS / authenticator / trusted device), STOP and finish with:  "
        "BLOCKED: <reason>\n"
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

# A portal rejecting the submit because the PRIMARY identity already hit its cap
# ("You have reached your application limit", "you've already applied"). This is a
# server-side block, not a fillable-field error — the owner asked that we re-try
# such cases with a pre-approved SECOND profile (alternate email + phone).
_LIMIT_RX = (r"reached your application limit|application limit|already applied"
             r"|already submitted an application|limit applications|maximum number of applications")


def _fallback_identity() -> dict:
    """The owner's SECOND profile — an alternate email + phone used to re-submit
    when a portal blocks the primary identity with an application-limit / already-
    applied message. Read from .local/secrets.json (key 'fallback_profile'), with
    env overrides (APPLIEDIN_FALLBACK_EMAIL / _PHONE). Empty dict = not configured,
    in which case we never swap. We only ever use a pre-approved identity — never
    an invented one."""
    import os
    from pathlib import Path

    from core.config import get_settings

    ident: dict = {}
    try:
        from core.storage.local import FileSecrets

        fs = FileSecrets(Path(get_settings().local_dir) / "secrets.json")
        ident = fs.get_json("fallback_profile") or {}
    except Exception:  # noqa: BLE001 - cloud mode / missing file: just no fallback
        ident = {}
    email = os.environ.get("APPLIEDIN_FALLBACK_EMAIL") or ident.get("Email") or ident.get("email")
    phone = os.environ.get("APPLIEDIN_FALLBACK_PHONE") or ident.get("Phone") or ident.get("phone")
    out: dict = {}
    if email:
        out["Email"] = email
    if phone:
        out["Phone"] = phone
    return out


async def _resubmit_with_fallback(page, pk: str, url: str) -> bool:  # noqa: ANN001
    """A portal blocked the primary identity (application-limit / already-applied).
    Overwrite Email + Phone with the owner's configured SECOND profile so a re-submit
    goes out under the alternate identity. Returns True if a swap was applied (caller
    then re-clicks submit), False if no fallback is configured. Dismisses any blocking
    overlay first so the form is interactable again."""
    ident = _fallback_identity()
    if not ident:
        return False
    _emit(pk, "response", agent="browser", url=url,
          detail=f"⚠ application limit hit — retrying with fallback profile "
                 f"({ident.get('Email', 'alternate identity')})")
    try:
        await page.keyboard.press("Escape")  # close the limit overlay if it's modal
    except Exception:  # noqa: BLE001
        pass
    await page.wait_for_timeout(300)
    await page.evaluate(_FILL_JS, ident)  # label-match overwrites even autofilled fields
    await page.wait_for_timeout(600)
    return True


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
    text_only = _is_text_only(model)
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


async def _agent_finish_submit(page, facts: dict, drafted: dict, model: str, pk: str,  # noqa: ANN001
                               url: str, upload_path: str, headed: bool, *, company: str,
                               jd_text: str, resume_tex: str, github: str) -> dict:
    """When the deterministic submit loop can't close a dynamic form (Ashby wipes
    fields on re-render; a React combobox hides its value from our DOM read), hand
    the FINISH + SUBMIT to a bounded browser-use agent on the SAME page. It has
    vision ground-truth we don't, so it knows when the form is really ready and can
    SEE the real confirmation. 'applied' still requires the confirmation text on the
    live page — we never trust the agent's word alone. A CAPTCHA/account wall hands
    off to you; a genuinely unknown field gates. Never solves a CAPTCHA."""
    import re as _re

    try:
        from browser_use import Agent

        from tools.browser_llm import make_llm
    except Exception as exc:
        return {"status": "uncertain", "detail": f"browser-use unavailable ({exc})"}
    m = model.lower()
    text_only = _is_text_only(model)
    facts_str = "\n".join(f"- {q}: {a}" for q, a in {**facts, **drafted}.items() if a) or "(none)"
    resume_line = ("Attach the REQUIRED résumé with the upload_resume action (NOT the "
                   "optional 'Autofill from resume' box); confirm its filename shows.\n"
                   if upload_path else "")
    snapshot: list = []
    task = (
        f"This {company} job-application form is already open and mostly filled. "
        f"FINISH and SUBMIT it (under 14 steps).\n{resume_line}"
        f"Approved answers — type these VERBATIM only, never invent:\n{facts_str}\n\n"
        "1. If a required field is empty, set it: fill_fields for text/dropdown, "
        "select_choices for radios/checkboxes; for an autocomplete (Location) click "
        "it, type the city, and click the matching suggestion (a more-qualified "
        "option like 'Seattle, Washington, United States' for 'Seattle' IS correct).\n"
        "2. call verify_form_filled and fix only what it flags.\n"
        "3. Click Submit/Apply and watch the page.\n"
        "4. If you SEE a confirmation ('application submitted', 'thank you'), finish "
        "with:  APPLIED: <exact confirmation text>\n"
        "Rules — never invent a value. If a required field has no approved answer, "
        "STOP with:  MISSING: <question>. If a CAPTCHA challenge or a login/account "
        "wall blocks submit, STOP with:  BLOCKED: <captcha or account> (never solve a "
        "CAPTCHA). If you clicked submit but see no confirmation, STOP with:  "
        "UNCERTAIN: <what the page shows>.")
    controller = _apply_controller(upload_path, pk, url, snapshot_sink=snapshot,
                                   drafted_sink=drafted, company=company, jd_text=jd_text,
                                   resume_tex=resume_tex, github=github)
    opts = {"llm": make_llm(model), "page": page, "use_vision": not text_only,
            "max_actions_per_step": 2, "register_new_step_callback": _step_cb(pk, url),
            "extend_system_message": _APPLY_POLICY,
            "available_file_paths": [upload_path] if upload_path else None}
    if controller is not None:
        opts["controller"] = controller
    _emit(pk, "response", agent="browser", url=url,
          detail="🤖 browser-use finishing + submitting the form (vision)")
    text, shot = "", None
    try:
        history = await Agent(task=task, **opts).run(max_steps=16)
        text = (history.final_result() if hasattr(history, "final_result") else str(history)) or ""
        shot = _last_screenshot(history)
    except Exception as exc:
        if _page_gone(exc):
            return {"status": "uncertain",
                    "detail": "the browser closed during submit — verify on the portal"}
        log.warning("agent finish+submit failed: %s", exc)

    # Ground truth: only call it applied if the LIVE page shows confirmation.
    try:
        body = (await page.evaluate("() => document.body.innerText || ''"))[:6000]
    except Exception:
        body = ""
    conf_m = _re.search(_SUCCESS_RX, body, _re.I) or _re.search(_SUCCESS_RX, text, _re.I)
    fields = _ui_fields(snapshot)
    if conf_m:
        line = next((ln.strip() for ln in body.splitlines()
                     if conf_m.group(0).lower() in ln.lower()), conf_m.group(0))
        return {"status": "applied", "confirmation": line[:120], "screenshot_b64": shot,
                "drafted": drafted or None, "fields": fields}
    up = text.upper()
    if "BLOCKED:" in up:
        q = text.split("BLOCKED:", 1)[1].strip()[:200]
        reason = "captcha" if "captcha" in q.lower() else "no_account"
        return {"status": "gate", "reason": reason, "screenshot_b64": shot,
                "drafted": drafted or None, "fields": fields, "question": q}
    if "MISSING:" in up:
        q = text.split("MISSING:", 1)[1].strip()[:200]
        return {"status": "gate", "reason": "unknown_field", "screenshot_b64": shot,
                "drafted": drafted or None, "fields": fields,
                "question": f"I need an answer for: {q}"}
    return {"status": "uncertain", "screenshot_b64": shot, "drafted": drafted or None,
            "fields": fields,
            "detail": (text[:200] or "browser-use finished without a visible confirmation")}


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
            """Hold the VISIBLE window open for the human ONLY when it's a genuine
            MUST — a CAPTCHA to solve or a login/2FA wall to sign in. Everything
            else (an uncertain submit, a field we couldn't fill) returns as-is with
            its screenshot, to be reviewed async on the 'Unable to do it' board —
            we never pull you into a live window unless you're truly required."""
            if not (headed and get_settings().assist_captcha):
                return res
            if res.get("reason") not in ("captcha", "no_account", "unknown_field"):
                return res  # not actionable in a window — surface it on the board
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
            # Belt-and-braces: a location field goes through the real-keystroke
            # pass even when the combo heuristic missed it — synthetic fill can
            # never commit a geocoder widget.
            combos = {f["label"]: fill_map[f["label"]] for f in fields
                      if (f.get("combo") or "location" in (f.get("label") or "").lower())
                      and f.get("type") not in ("choice-group",)
                      and f.get("label") in fill_map}
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

            # Last fill step before the submit loop: click-commit every box/text field.
            await _click_fields_pass(page, pk, url)

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
            # ReAct submit loop: submit → read the FORM's OWN validation errors →
            # fix the flagged fields (browser-use for stubborn autocompletes) →
            # RESUBMIT and let the form confirm. The form is the source of truth —
            # we never gate on our own DOM read (a React combobox stores its value
            # off the visible input, so reading it as "empty" would falsely gate a
            # field that is in fact set).
            body, shot, errors, state = "", None, [], []
            _MAX_SUBMITS, handed_off, swapped = 3, False, False
            for attempt in range(_MAX_SUBMITS):
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
                # Application-limit block is checked BEFORE success: a portal's limit
                # page can literally say "Thank you for considering Ramp!" which
                # matches _SUCCESS_RX — so it must be ruled out first or a rejection
                # gets mislabeled as an applied. Retry ONCE with the next profile.
                if _re.search(_LIMIT_RX, body, _re.I):
                    if not swapped and await _resubmit_with_fallback(page, pk, url):
                        swapped = True
                        continue  # re-click submit under the alternate identity
                    line = next((ln.strip() for ln in body.splitlines()
                                 if _re.search(_LIMIT_RX, ln, _re.I)), "Application limit reached.")
                    note = (" Retried with your fallback profile but it's still blocked."
                            if swapped else " No fallback profile is configured to retry with.")
                    return {"status": "failed", "reason": "application_limit",
                            "detail": f"The portal blocked this application (limit reached): "
                                      f"{line[:180]}.{note}",
                            "screenshot_b64": shot, "drafted": drafted or None,
                            "fields": _ui_fields(state)}
                m = _re.search(_SUCCESS_RX, body, _re.I)
                url_ok = page.url != url and bool(_re.search(
                    r"confirmation|submitted|thank|success|application-complete", page.url, _re.I))
                if m or url_ok:
                    line = (next((ln.strip() for ln in body.splitlines()
                                  if m and m.group(0).lower() in ln.lower()), None)
                            or (m.group(0) if m else "Application submitted."))
                    return {"status": "applied", "confirmation": line[:120],
                            "screenshot_b64": shot, "drafted": drafted or None,
                            "fields": _ui_fields(state)}
                # Server-validated portals (classic Lever) RELOAD the page on a
                # rejected submit, wiping EVERY field — even ones that were fine.
                # Detect the mass-wipe and refill the whole form before judging
                # errors, or the gate blames fields we actually had answers for.
                if attempt < _MAX_SUBMITS - 1:
                    labels_now = {(f.get("label") or "").strip(): (f.get("value") or "")
                                  for f in state}
                    wiped = [k for k in fill_map if labels_now.get(k.strip()) == ""]
                    if len(wiped) >= max(2, len(fill_map) // 2):
                        _emit(pk, "response", agent="browser", url=url,
                              detail=f"🔄 submit reset the form — refilling "
                                     f"{len(wiped)} wiped field(s)")
                        await page.evaluate(_FILL_JS, fill_map)
                        await page.wait_for_timeout(700)
                        if choice_map:
                            await _set_choices(page, choice_map, pk, url)
                        if combos:
                            await _fix_fields(page, combos, pk, url)
                        await _click_fields_pass(page, pk, url)
                        continue
                errors = await page.evaluate(_ERRORS_JS)
                if not errors:
                    break  # the FORM is satisfied — CAPTCHA / final check next
                if attempt == _MAX_SUBMITS - 1:
                    break  # out of attempts; gate below on the form's remaining errors
                # OBSERVE: which rejected fields do we actually have a value for?
                fix_map: dict[str, str] = {}
                unknown: list[str] = []
                for err in errors:
                    hit = next(((k, v) for k, v in fill_map.items()
                                if k.lower() in err.lower()), None) or _fact_for(err, facts)
                    if hit:
                        k, v = hit
                        fix_map[k] = v.split(",")[0].strip() if "location" in err.lower() else v
                    else:
                        unknown.append(err)
                if not fix_map:  # nothing we can auto-fill — genuinely needs you
                    return await _maybe_assist({
                        "status": "gate", "reason": "unknown_field",
                        "screenshot_b64": shot, "drafted": drafted or None,
                        "fields": _ui_fields(state),
                        "question": "Please set these in the open window: "
                                    + "; ".join(unknown or errors)})
                # ACT: fix deterministically; hand stubborn fields to a short vision
                # browser-use agent on THIS page ONCE. Then loop → RESUBMIT so the
                # form (not our read) decides whether the fix took.
                _emit(pk, "response", agent="browser", url=url,
                      detail=f"🔧 fixing rejected fields: {list(fix_map)[:4]}")
                await _fix_fields(page, fix_map, pk, url)
                await page.wait_for_timeout(800)
                if not handed_off:
                    still = (await _still_empty(list(fix_map)))[0]
                    residual = {k: fix_map[k] for k in still if fix_map.get(k)}
                    if residual:
                        await _agent_finish_residuals(page, residual, model, pk, url,
                                                      company=company, jd_text=jd_text,
                                                      resume_tex=resume_tex, github=github)
                        handed_off = True

            if errors:  # deterministic loop couldn't close this (dynamic) form →
                # hand the finish+submit to a vision browser-use agent on this page.
                res = await _agent_finish_submit(
                    page, {**facts, **drafted}, drafted, model, pk, url, upload_path,
                    headed, company=company, jd_text=jd_text, resume_tex=resume_tex,
                    github=github)
                return await _maybe_assist(res)

            # The form accepted the submit (no errors). Scan for a required field we
            # can RELIABLY read as empty — but skip comboboxes: their value lives off
            # the visible input, so trust the form's verdict over our read for those.
            state = await page.evaluate(_READ_FORM_JS)
            empty_req = [f for f in state if f.get("required") and not f.get("value")
                         and f.get("type") != "file" and not f.get("combo")]
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

    # No expiry by owner request: the window stays open and watched (6h horizon
    # as a leak backstop) — closing the window is how the human releases it.
    secs = int(getattr(get_settings(), "assist_wait_seconds", 21600))
    if reason == "captcha":
        ask = "solve the CAPTCHA and click Submit"
    elif note:
        ask = f"{note.rstrip('.')}, then click Submit"
    else:
        ask = "finish anything the form still needs and click Submit"
    _emit(pk, "gate", agent="applier", url=url,
          detail=f"🙋 Over to you: the application is filled in the open Chrome "
                 f"window — {ask}. No rush: the window stays open and I'll watch "
                 f"until you finish (or close it).")
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


_HAS_OPTIONS_JS = (
    "() => !!document.querySelector('[role=option], [role=listbox] li, "
    "[class*=\"dropdown\" i] li, [class*=\"suggestion\" i], [class*=\"result\" i] li')")

# The widget's own status line — visible short text like "Loading…" or
# "No location found. Try entering a different location" — tells us whether to
# keep waiting (async lookup in flight) or retry with a different query.
_COMBO_STATUS_JS = """
() => {
  const vis = e => !!(e.offsetParent !== null || e.getClientRects().length);
  const txts = [...document.querySelectorAll('div, span, li, p, em')]
    .filter(e => vis(e) && !e.querySelector('input,textarea,select')
                 && (e.textContent || '').length < 90)
    .map(e => (e.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase());
  return {
    loading: txts.some(t => /^(loading|searching)(…|\\.{3})?$/.test(t)),
    empty: txts.some(t => /no .{0,12}(found|results?|matches)|try entering a different/.test(t)),
  };
}
"""


async def _fill_combobox(page, label: str, value: str, pk: str = "", url: str = "") -> bool:  # noqa: ANN001
    """Robustly fill an autocomplete/combobox (Ashby/Lever Location, school
    pickers) — the class of field synthetic value-setting can't touch. Locates
    the REAL input by its resolved label, clicks it, types with actual
    keystrokes, then WAITS OUT the widget's async 'Loading' state before
    judging the suggestion list (a fixed short poll used to expire mid-geocode).
    On a hard 'no results' it retries with a shorter prefix, and when the
    QUESTION itself mandates a fallback option (Lever: «Please select "Other
    (School Not Listed)" if …») it picks that before giving up. Handles plain
    text inputs too (types the value, no pick). Returns True iff the field
    ended up with a value."""
    import re as _re

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
            || e.getAttribute('aria-controls') || e.closest('[role=combobox]')
            || /location|typeahead|autocomplete/i.test(
                 (e.className || '') + ' ' + (e.name || '') + ' ' + (e.id || '')))""")
        # A city picker wants just the city token; a plain field wants the full value.
        typed = value.split(",")[0].strip() if (is_auto and "," in value) else value
        # Strict dropdowns render every option on click — try a pick before typing.
        picked = await page.evaluate(_PICK_OPTION_JS, [value, typed])
        if picked:
            _emit(pk, "response", agent="browser", url=url, detail=f"📍 picked: {picked}")
            return True
        # Type → wait out 'Loading' → pick; on a hard 'no results', retry with a
        # shorter prefix (a widget can reject the full string yet match its head).
        saw_dropdown = False
        queries = [typed] + ([typed[:5]] if is_auto and len(typed) > 5 else [])
        for q in queries:
            try:
                await el.fill("", timeout=1500)
            except Exception:
                pass
            await el.type(q, delay=40, timeout=6000)
            await page.wait_for_timeout(450)
            idle = 0
            for _ in range(16):  # ≤ ~8s per query — geocoders can be slow
                picked = await page.evaluate(_PICK_OPTION_JS, [value, typed, q])
                if picked:
                    _emit(pk, "response", agent="browser", url=url, detail=f"📍 picked: {picked}")
                    await page.wait_for_timeout(200)
                    return True
                st = await page.evaluate(_COMBO_STATUS_JS)
                has_opts = await page.evaluate(_HAS_OPTIONS_JS)
                if has_opts or st.get("loading") or st.get("empty"):
                    saw_dropdown = True
                if st.get("loading"):
                    await page.wait_for_timeout(500)
                    continue  # suggestions are still on their way — keep waiting
                if st.get("empty"):
                    break  # widget says 'no results' → try the shorter prefix
                if has_opts:
                    idle = 0
                else:
                    idle += 1
                    if idle >= 4:
                        break  # nothing is appearing — plain field or dead widget
                await page.wait_for_timeout(400)
        val = (await el.evaluate("e => (e.value || '').trim()")) or ""
        if not is_auto and val:
            return True  # plain input: the typed value stuck
        if is_auto and not saw_dropdown:
            # The widget never opened anything — the combo heuristic likely
            # over-matched a plain input; keep the typed text, never wipe it.
            return bool(val)
        if is_auto:
            # The question may mandate its own escape hatch («Please select
            # "Other (School Not Listed)" if your school is not listed») —
            # honor it before giving up on the field.
            m = _re.search(r'select\s+["“]([^"”]{3,60})["”]', label, _re.I)
            if m:
                fb = m.group(1).strip()
                try:
                    await el.fill("", timeout=1500)
                    await el.type(fb.split()[0], delay=40, timeout=6000)
                except Exception:
                    pass
                for _ in range(10):
                    await page.wait_for_timeout(400)
                    picked = await page.evaluate(_PICK_OPTION_JS, [fb])
                    if picked:
                        _emit(pk, "response", agent="browser", url=url,
                              detail=f"📍 fallback picked: {picked}")
                        return True
            # Keyboard-commit the first FILTERED suggestion (handles widgets whose
            # options aren't [role=option]) — but ONLY when a suggestion list is
            # actually visible (Enter on an empty/loading list commits nothing),
            # and ACCEPT it only if what committed actually matches the city.
            # Never blind-commit a random first item (that submits the wrong
            # city, e.g. "Twelve Mile, Indiana").
            if await page.evaluate(_HAS_OPTIONS_JS):
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
  const _short = t => { t = norm(t); return t && t.length < 80 ? t : ''; };
  const optLabel = el => _short((el.labels && el.labels[0] && el.labels[0].textContent) || '')
    || _short((el.closest('label') || {}).textContent || '')
    || _short(el.nextElementSibling && el.nextElementSibling.textContent || '')
    || _short(el.parentElement && el.parentElement.textContent || '')
    || norm(el.value);
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
    // ARIA first; then bare widgets with no ARIA at all — classic Lever's
    // location geocoder is a plain <input class="location-input"> whose only
    // tell is its class/name/id, and missing it meant synthetic fill + a gate.
    const combo = !!(el.getAttribute('role') === 'combobox'
                     || el.getAttribute('aria-haspopup')
                     || el.getAttribute('aria-autocomplete')
                     || el.closest('[role="combobox"]')
                     || /start typing/i.test(el.placeholder || '')
                     || (el.tagName !== 'SELECT' && (el.type || '') !== 'file'
                         && /location|typeahead|autocomplete/i.test(
                              (el.className || '') + ' ' + (el.name || '') + ' ' + (el.id || ''))));
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
          if (!isText(el) || ntrim(el.value)) continue;  // fuzzy also covers SELECTs now
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
  const short = t => { t = norm(t); return t && t.length < 80 ? t : ''; };
  const nq = norm(q), nv = norm(v);
  if (!nq || !nv) return 'NOQUESTION';
  const qnode = [...document.querySelectorAll('legend,label,p,span,div,h3,h4')]
    .filter(e => !e.querySelector('input') && norm(e.textContent).includes(nq))
    .sort((a, b) => a.textContent.length - b.textContent.length)[0];  // tightest node
  if (!qnode) return 'NOQUESTION';
  let scope = qnode.closest('fieldset') || qnode.parentElement;
  for (let i = 0; i < 6 && scope; i++) {
    const inputs = [...scope.querySelectorAll('input[type=radio], input[type=checkbox]')];
    if (inputs.length) {
      const optLbl = inp => short((inp.labels && inp.labels[0] && inp.labels[0].textContent) || '')
        || short((inp.closest('label') || {}).textContent || '')
        || short(inp.nextElementSibling && inp.nextElementSibling.textContent || '')
        || short(inp.parentElement && inp.parentElement.textContent || '')
        || norm(inp.value);
      const hit = inputs.find(inp => { const l = optLbl(inp);
        return l && (l === nv || l.includes(nv) || nv.includes(l)); });
      if (!hit) return 'NOMATCH options: ' + inputs.slice(0, 12).map(optLbl)
        .filter(Boolean).join(' | ').slice(0, 200);
      if (hit.checked) return 'ALREADY';
      return (hit.labels && hit.labels[0]) || hit.closest('label') || hit;
    }
    scope = scope.parentElement;
  }
  return 'NOQUESTION';
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


_CLICK_PASS_JS = """
() => {
  const els = [...document.querySelectorAll('input, textarea')];
  let n = 0;
  for (const el of els) {
    const t = (el.type || '').toLowerCase();
    // Only plain boxes/text fields — a click on any of these merely focuses.
    if (['hidden', 'file', 'checkbox', 'radio', 'submit', 'button', 'reset',
         'image', 'range', 'color'].includes(t)) continue;
    if (el.disabled || el.readOnly) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    // Combobox/autocomplete widgets: a click re-opens the suggestion menu and
    // the blur can wipe the option we already picked — never touch them here.
    // ARIA first; then bare widgets with no ARIA at all — classic Lever's
    // location geocoder is a plain <input class="location-input"> whose only
    // tell is its class/name/id, and missing it meant synthetic fill + a gate.
    const combo = !!(el.getAttribute('role') === 'combobox'
                     || el.getAttribute('aria-haspopup')
                     || el.getAttribute('aria-autocomplete')
                     || el.closest('[role="combobox"]')
                     || /start typing/i.test(el.placeholder || '')
                     || (el.tagName !== 'SELECT' && (el.type || '') !== 'file'
                         && /location|typeahead|autocomplete/i.test(
                              (el.className || '') + ' ' + (el.name || '') + ' ' + (el.id || ''))));
    if (combo) continue;
    el.setAttribute('data-appliedin-clickpass', String(n++));
  }
  return n;
}
"""


async def _click_fields_pass(page, pk: str, url: str) -> int:  # noqa: ANN001
    """The LAST step of the fill phase (owner requirement): one REAL click on
    every box/text field after the whole application is filled. Each click
    focuses its field and thereby BLURS the previous one, firing the
    change/blur handlers dynamic (React) forms need to commit a value.
    Skips anything a click would mutate or pop open: checkboxes/radios
    (toggle), file inputs (OS dialog), combobox/autocomplete widgets (menu
    could wipe the picked value). Best-effort — a failed click never blocks
    the submit."""
    try:
        total = await page.evaluate(_CLICK_PASS_JS)
        clicked = 0
        for i in range(total):
            loc = page.locator(f'[data-appliedin-clickpass="{i}"]').first
            try:
                await loc.scroll_into_view_if_needed(timeout=1500)
                await loc.click(timeout=2000)
                clicked += 1
            except Exception:
                continue
        await page.evaluate(
            "() => { document.querySelectorAll('[data-appliedin-clickpass]')"
            ".forEach(el => el.removeAttribute('data-appliedin-clickpass'));"
            " const a = document.activeElement; if (a && a.blur) a.blur(); }")
        if total:
            _emit(pk, "response", agent="browser", url=url,
                  detail=f"🖱 final pass: clicked {clicked}/{total} box/text field(s)")
        return clicked
    except Exception as exc:
        log.debug("click-fields pass skipped: %s", exc)
        return 0


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

    body, shot, swapped = "", None, False
    for attempt in (0, 1, 2):
        clicked = await page.evaluate(_SUBMIT_JS)
        if clicked:
            _emit(pk, "response", agent="browser", url=url, detail=f"⏎ clicked: {clicked}")
        await page.wait_for_timeout(5000)
        body = (await page.evaluate("() => document.body.innerText || ''"))[:6000]
        shot = base64.b64encode(await page.screenshot()).decode()
        state = await page.evaluate(_READ_FORM_JS)
        # Limit block BEFORE success — the limit page can contain "thank you",
        # which would otherwise match _SUCCESS_RX and mislabel a rejection as applied.
        if _re.search(_LIMIT_RX, body, _re.I):
            if not swapped and await _resubmit_with_fallback(page, pk, url):
                swapped = True
                continue
            line = next((ln.strip() for ln in body.splitlines()
                         if _re.search(_LIMIT_RX, ln, _re.I)), "Application limit reached.")
            note = (" Retried with your fallback profile but it's still blocked."
                    if swapped else " No fallback profile is configured to retry with.")
            return {"status": "failed", "reason": "application_limit", "screenshot_b64": shot,
                    "detail": f"The portal blocked this application (limit reached): "
                              f"{line[:180]}.{note}",
                    "drafted": drafted or None, "fields": _ui_fields(state)}
        m = _re.search(_SUCCESS_RX, body, _re.I)
        if m:
            line = next((ln.strip() for ln in body.splitlines()
                         if m.group(0).lower() in ln.lower()), m.group(0))
            return {"status": "applied", "confirmation": line[:120], "screenshot_b64": shot,
                    "drafted": drafted or None, "fields": _ui_fields(state)}
        # Server-validated portals (classic Lever) RELOAD the page on a rejected
        # submit, wiping EVERY field — even ones that were fine. Rebuild a fix
        # map for ALL empty required fields from the facts and restore text,
        # choices AND combos, then loop to resubmit; without this the terminal
        # gate blames fields we actually had answers for.
        if attempt < 2:
            empty_now = [f for f in state if f.get("required") and not f.get("value")
                         and f.get("type") not in ("file",)]
            text_fix, choice_fix2, combo_fix = {}, {}, {}
            for f in empty_now:
                hit = _fact_for(f.get("label", ""), facts)
                if not hit:
                    continue
                _, v = hit
                if f.get("type") == "choice-group":
                    choice_fix2[f["label"]] = v
                elif f.get("combo"):
                    combo_fix[f["label"]] = v
                else:
                    text_fix[f["label"]] = v
            if len(text_fix) + len(choice_fix2) + len(combo_fix) >= 2:
                _emit(pk, "response", agent="browser", url=url,
                      detail=f"🔄 submit reset the form — restoring "
                             f"{len(text_fix) + len(choice_fix2) + len(combo_fix)} field(s)")
                if text_fix:
                    await page.evaluate(_FILL_JS, text_fix)
                    await page.wait_for_timeout(700)
                if choice_fix2:
                    await _set_choices(page, choice_fix2, pk, url)
                if combo_fix:
                    await _fix_fields(page, combo_fix, pk, url)
                await _click_fields_pass(page, pk, url)
                continue
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
    # Only hold the window open for a REAL CAPTCHA (a genuine must). An empty field
    # or an unconfirmed submit falls through to a board gate (with a screenshot) —
    # we don't pull the human into a live window unless one is truly required.
    if headed and get_settings().assist_captcha and captcha:
        conf = await _assist_wait(page, pk, url, "", "captcha")
        if conf:
            st = await page.evaluate(_READ_FORM_JS)
            return {"status": "applied", "confirmation": conf,
                    "screenshot_b64": base64.b64encode(await page.screenshot()).decode(),
                    "drafted": drafted or None, "fields": _ui_fields(st)}
    if empty_req:
        labels = [f["label"] for f in empty_req]
        # The gate says "in the window" — so actually HOLD the window open (no
        # expiry) and watch for the human to finish, instead of killing it and
        # pointing at a window that no longer exists.
        if headed and get_settings().assist_captcha:
            conf = await _assist_wait(
                page, pk, url,
                "set " + "; ".join(l[:60] for l in labels[:4]), "")
            if conf:
                st = await page.evaluate(_READ_FORM_JS)
                return {"status": "applied", "confirmation": conf,
                        "screenshot_b64": base64.b64encode(await page.screenshot()).decode(),
                        "drafted": drafted or None, "fields": _ui_fields(st)}
        return {"status": "gate", "reason": "unknown_field", "screenshot_b64": shot,
                "drafted": drafted or None, "fields": _ui_fields(state),
                "question": "Set these and submit in the window: " + "; ".join(labels[:5])}
    if captcha:
        return {"status": "gate", "reason": "captcha", "screenshot_b64": shot,
                "drafted": drafted or None, "fields": _ui_fields(state),
                "question": "All fields filled — only the CAPTCHA remains. Solve it and submit."}
    return {"status": "uncertain", "detail": "submit clicked; no confirmation appeared",
            "screenshot_b64": shot, "drafted": drafted or None, "fields": _ui_fields(state)}


async def _set_choices(page, choices: dict, pk: str = "", url: str = "") -> dict:  # noqa: ANN001
    """Select radio/checkbox options with REAL Playwright clicks — the only thing
    React groups (Ashby) accept. `choices` maps question -> option (str OR list for
    'select all that apply'). Verifies each click by re-locating (ALREADY = success),
    never clicks a wrong option, and never re-toggles an already-checked box. Returns
    {question: 'set' | failure-detail}."""
    out = {}
    for q, vals in choices.items():
        vals = vals if isinstance(vals, list) else [vals]
        statuses = []
        for v in vals:
            try:
                h = await page.evaluate_handle(_LOCATE_CHOICE_JS, {"q": q, "v": str(v)})
                el = h.as_element()
                if el is None:
                    statuses.append(str(await h.json_value() or "NOQUESTION"))
                    continue
                await el.scroll_into_view_if_needed(timeout=2000)
                await el.click(timeout=3000)
                await page.wait_for_timeout(250)
                h2 = await page.evaluate_handle(_LOCATE_CHOICE_JS, {"q": q, "v": str(v)})
                statuses.append("set" if (h2.as_element() is None
                                and await h2.json_value() == "ALREADY") else "click-no-effect")
            except Exception as exc:  # noqa: BLE001
                statuses.append(f"error:{exc}"[:120])
        good = [s for s in statuses if s in ("set", "ALREADY")]
        out[q] = "set" if (statuses and len(good) == len(statuses)) else "; ".join(statuses)[:200]
    if pk and any(s == "set" for s in out.values()):
        _emit(pk, "response", agent="browser", url=url,
              detail=f"◉ selected {sum(1 for s in out.values() if s == 'set')} choice question(s)")
    return out


_PICK_OPTION_JS = """
(wanted) => {
  const norm = s => (s || '').toLowerCase().replace(/\\s+/g, ' ').trim();
  const vals = (wanted || []).map(norm).filter(Boolean);
  if (!vals.length) return '';
  // Never click a form-action control — only a suggestion whose text matches a
  // value we're setting (either direction: 'Seattle' ⊂ 'Seattle, WA, USA').
  const ACTION = /^(submit|apply|continue|next|back|previous|cancel|close|clear|search|save|done|remove|upload|browse|add|sign|log)\\b/;
  // Short values ("No", "Yes") must match EXACTLY or as a whole word — plain
  // substring matching made "No" pick "Lebanon+961" in a phone-country list.
  const matches = t => t && t.length < 80 && !ACTION.test(t)
                       && vals.some(v => v.length >= 4
                         ? (v.includes(t) || t.includes(v))
                         : (t === v || t.split(/[^a-z0-9+]+/).includes(v)));
  // Prefer real listbox/menu options; then fall back to ANY visible clickable a
  // combobox popup might render suggestions as — Ashby uses <button> elements, so
  // '[role=option]/li'-only found nothing and Location never got picked.
  const structured = [...document.querySelectorAll(
    '[role="option"], [role="listbox"] li, [role="listbox"] button, ' +
    '[role="menu"] [role="menuitem"], [class*="option" i], [class*="suggestion" i], ' +
    '[class*="autocomplete" i] li, [class*="result" i] li, [class*="menu" i] li, ' +
    '[class*="dropdown" i] li')];
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

    # Per-job state so the actions return DURABLE receipts (long_term_memory is the
    # only field 0.5.9 persists — include_in_memory is dead) and so verify can trust
    # a combo we set even when the DOM reads it empty (the Ashby re-fill-loop fix).
    run_state = {"filled": {}, "verify_calls": 0, "last_missing": None,
                 "notfound_prev": set(), "unmatchable": set(), "choice_prev": set()}

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
        "Fetch a login/verification code the portal JUST EMAILED to the candidate. "
        "Call this when a sign-in wall says it emailed a code (check the wall's own "
        "words). Returns the digits to type, or NO_CODE when nothing has arrived — "
        "then wait ~20 seconds and call it ONCE more. Codes sent by SMS, an "
        "authenticator app, or to a trusted device ('sent to your Apple devices') "
        "can NEVER be fetched — report those as BLOCKED instead.")
    async def fetch_email_code() -> ActionResult:
        import asyncio

        from core.stores import make_stores
        from tools.gmail import fetch_code

        try:
            code = await asyncio.to_thread(
                fetch_code,
                'newer_than:1h (verification OR verify OR code OR passcode OR "one-time")',
                make_stores().secrets)
        except Exception as exc:
            log.warning("fetch_email_code failed: %s", exc)
            code = None
        _emit(pk, "response", agent="browser", url=url,
              detail=("📧 fetched an emailed verification code" if code
                      else "📧 no fresh verification code in the inbox"))
        return ActionResult(extracted_content=code or "NO_CODE",
                            include_in_memory=True)

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
        # 0) AUTO-SKIP set or provably-dead keys — a re-send returns instantly, no loop.
        skipped_bad = [k for k in mapping if k in run_state["unmatchable"]]
        mapping = {k: v for k, v in mapping.items()
                   if k not in run_state["filled"] and k not in run_state["unmatchable"]}
        if not mapping:
            return ActionResult(long_term_memory=(
                "fill_fields: NOTHING TO DO — every key you sent is already SET or UNMATCHABLE"
                + (f" (UNMATCHABLE: {', '.join(skipped_bad)[:250]})" if skipped_bad else "")
                + ". NEXT STEP: click any still-empty field directly on the page (ONE attempt "
                "each), then verify_form_filled, then click Submit."))
        try:
            page = await _page_of(browser_session)
            report = await page.evaluate(_FILL_JS, mapping)  # type: ignore[union-attr]
            # Autocomplete comboboxes (Location) open a list — commit the matching option,
            # but only while a popup is actually OPEN (never re-toggle already-set buttons).
            wanted = [v for v in mapping.values() if isinstance(v, str)]
            _POPUP_JS = ("() => !!document.querySelector('[role=\"listbox\"], [role=\"option\"], "
                         "[class*=\"popover\" i], [class*=\"autocomplete\" i] [class*=\"option\" i]')")
            for _ in range(3):
                await page.wait_for_timeout(700)  # type: ignore[union-attr]
                if not await page.evaluate(_POPUP_JS):  # type: ignore[union-attr]
                    break
                picked = await page.evaluate(_PICK_OPTION_JS, wanted)  # type: ignore[union-attr]
                if not picked:
                    break
                report.append(f"PICKED OPTION: {picked}")
        except Exception as exc:
            return ActionResult(extracted_content=f"fill_fields errored: {exc}",
                                include_in_memory=True)
        ok = [k for k in mapping if ("FILLED: " + k) in report]
        missed = [k for k in mapping if ("NOT FOUND: " + k) in report]
        # 1) RESCUE PASS: the model keys fields from the vision namespace, but _FILL_JS
        # matches the CANONICAL _READ_FORM_JS label — token-map each miss to a real page
        # label and re-fill (a canonical label always matches). Fixes the 23/31-miss loop.
        state = []
        if missed:
            state = await page.evaluate(_READ_FORM_JS)  # type: ignore[union-attr]
            canon = [f["label"] for f in state if not f["value"]]
            for k in list(missed):
                kt, best, score = _toks(k), None, 0.0
                for lbl in canon:
                    ov = len(kt & _toks(lbl)) / (len(kt) or 1)
                    if ov > score:
                        best, score = lbl, ov
                if best and score >= 0.5:
                    r2 = await page.evaluate(_FILL_JS, {best: mapping[k]})  # type: ignore[union-attr]
                    report += r2
                    if ("FILLED: " + best) in r2:
                        ok.append(k)
                        missed.remove(k)
        # 2) TWO-STRIKE: still NOT FOUND after a rescue AND missed last call → dead forever.
        run_state["unmatchable"].update(k for k in missed if k in run_state["notfound_prev"])
        run_state["notfound_prev"] = set(missed)
        run_state["filled"].update({k: mapping[k] for k in ok})
        mem = [f"fill_fields SET {len(ok)}/{len(mapping)}: "
               + "; ".join(f'{k}="{str(mapping[k])[:40]}"' for k in ok)[:400] + ". NEVER re-send a SET field."]
        live = [k for k in missed if k not in run_state["unmatchable"]]
        if live:
            empty_labels = [f["label"] for f in state if not f["value"]][:15]
            mem.append(f"NOT FOUND ({len(live)}): {', '.join(live)[:200]}. Retry these ONCE, but ONLY "
                       f"with keys copied EXACTLY from these real page labels: {' | '.join(empty_labels)[:400]}")
        if run_state["unmatchable"]:
            mem.append(f"UNMATCHABLE ({len(run_state['unmatchable'])}): "
                       f"{', '.join(sorted(run_state['unmatchable']))[:300]}. fill_fields FAILED TWICE on "
                       "these — it can NEVER set them. Do NOT send them again under ANY wording. Instead: "
                       "click each directly on the page (ONE attempt), then verify_form_filled and SUBMIT. "
                       "Only if submit is blocked by one, finish  MISSING: <label>.")
        _emit(pk, "response", agent="browser", url=url, detail=f"⚡ fill → {' | '.join(mem)}"[:240])
        return ActionResult(extracted_content="fill detail: " + "; ".join(report),
                            include_extracted_content_only_once=True,
                            long_term_memory=" | ".join(mem)[:1200])

    @controller.registry.action(
        "Select radio-button / checkbox answers. choices_json: a JSON object ENCODED "
        "AS A STRING mapping each question's EXACT text to the option to pick — for "
        "'select all that apply' the value may be a JSON ARRAY of option texts, e.g. "
        '\'{"Are you authorized…": "Yes", "Which country…": ["United States"]}\'. '
        "Uses REAL clicks React forms accept. Use THIS for every radio/checkbox question.")
    async def select_choices(choices_json: str, browser_session) -> ActionResult:  # noqa: ANN001
        try:
            mapping = json.loads(choices_json)
            if not isinstance(mapping, dict):
                raise ValueError("choices_json must encode a JSON object")
        except Exception as exc:
            return ActionResult(error=f"select_choices: bad json ({exc})")
        # (i) AUTO-SKIP set/dead questions — a disobedient re-send returns instantly.
        skipped_bad = [q for q in mapping if q in run_state["unmatchable"]]
        mapping = {q: v for q, v in mapping.items()
                   if q not in run_state["filled"] and q not in run_state["unmatchable"]}
        if not mapping:
            return ActionResult(long_term_memory=(
                "select_choices: NOTHING TO DO — every question is already SET or UNMATCHABLE"
                + (f" (UNMATCHABLE: {', '.join(skipped_bad)[:250]})" if skipped_bad else "")
                + ". NEXT: click any still-empty choice on the page directly (ONE attempt each), "
                "then verify_form_filled, then Submit."))
        page = await _page_of(browser_session)
        status = await _set_choices(page, mapping, pk, url)
        ok = [q for q, s in status.items() if s == "set"]
        failed = {q: s for q, s in status.items() if s != "set"}
        # (iii) RESCUE: re-key question to the canonical choice-group label, re-map the
        # value to a REAL option text, and retry — fixes the paraphrased-key misses.
        if failed:
            groups = [f for f in await page.evaluate(_READ_FORM_JS) if f.get("type") == "choice-group"]
            for q in list(failed):
                kt, best, score = _toks(q), None, 0.0
                for g in groups:
                    ov = len(kt & _toks(g["label"])) / (len(kt) or 1)
                    if ov > score:
                        best, score = g, ov
                if not best or score < 0.5:
                    continue
                raw = mapping[q] if isinstance(mapping[q], list) else [mapping[q]]
                fixed = [next((o for o in best.get("options", [])
                               if str(v).lower() in o.lower() or o.lower() in str(v).lower()), str(v))
                         for v in raw]
                if (await _set_choices(page, {best["label"]: fixed}, pk, url)).get(best["label"]) == "set":
                    ok.append(q)
                    failed.pop(q)
                    run_state["filled"][best["label"]] = fixed  # canonical key → verify overlay hits
        # (ii) TWO-STRIKE
        run_state["unmatchable"].update(q for q in failed if q in run_state["choice_prev"])
        run_state["choice_prev"] = set(failed)
        run_state["filled"].update({q: mapping[q] for q in ok})
        mem = [f"select_choices SET {len(ok)}/{len(status)}: "
               + "; ".join(f'{q}="{mapping[q]}"' for q in ok)[:400] + ". NEVER re-send a SET question."]
        live = {q: s for q, s in failed.items() if q not in run_state["unmatchable"]}
        if live:
            mem.append("FAILED: " + "; ".join(f"{q} -> {s}" for q, s in live.items())[:400]
                       + ". Retry ONCE, copying the question AND option text EXACTLY as shown on the "
                       "page (the '-> NOMATCH options:' list shows the REAL option texts).")
        if run_state["unmatchable"]:
            mem.append(f"UNMATCHABLE: {', '.join(sorted(run_state['unmatchable']))[:300]} — FAILED "
                       "TWICE; NEVER send these to select_choices again. Click each directly on the "
                       "page (ONE attempt), then verify_form_filled and SUBMIT.")
        return ActionResult(long_term_memory=" | ".join(mem)[:1200])

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
            return ActionResult(error=f"verify errored: {exc}")
        if snapshot_sink is not None:  # keep the latest reading for the dashboard
            snapshot_sink[:] = fields
        run_state["verify_calls"] += 1
        # ONLY the remaining problems (never list OK/optional fields — that invites
        # re-fills). A combo we already SET but the DOM reads empty (Ashby commits to
        # a hidden input and clears the visible one) is NOT a problem — trust our receipt.
        empty_req = [f["label"] for f in fields
                     if f["required"] and not f["value"]
                     and f["type"] not in ("radio", "file")
                     and not (f.get("combo") and run_state["filled"].get(f["label"]))]
        n_ok = sum(1 for f in fields if f["value"])
        if not empty_req:
            mem = (f"verify #{run_state['verify_calls']}: ALL REQUIRED FIELDS OK ({n_ok} "
                   "filled). Form is COMPLETE — click Submit NOW. Do not fill anything else.")
        elif run_state["verify_calls"] >= 3 or empty_req == run_state["last_missing"]:
            mem = (f"verify #{run_state['verify_calls']}: still flags "
                   f"{', '.join(empty_req)[:200]} but the repair budget is EXHAUSTED. STOP "
                   "repairing — click Submit NOW and use the page's own validation errors, "
                   "or finish with UNCERTAIN. Do NOT call fill_fields again.")
        else:
            run_state["last_missing"] = empty_req
            mem = (f"verify #{run_state['verify_calls']}: fix ONLY these {len(empty_req)} "
                   f"required fields: {', '.join(empty_req)[:300]}. ALL {n_ok} other fields "
                   "are OK — never re-touch them.")
        _emit(pk, "response", agent="browser", url=url, detail=f"🔍 pre-submit check: {mem}"[:240])
        return ActionResult(long_term_memory=mem)

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
        # Ashby/Greenhouse AUTO-FILL name/email/phone from the résumé (async). Wait,
        # then read the form and record what got auto-filled so fill_fields SKIPS
        # them — never re-type an auto-filled field.
        autofilled = []
        try:
            await page.wait_for_timeout(1800)  # type: ignore[union-attr]
            for f in await page.evaluate(_READ_FORM_JS):  # type: ignore[union-attr]
                if f.get("value") and f.get("type") not in ("file", "radio", "checkbox"):
                    run_state["filled"][f["label"]] = f["value"]
                    autofilled.append(f["label"])
        except Exception:  # noqa: BLE001
            pass
        msg = (f"Attached the résumé to {n} field(s)." if n
               else "No résumé file input was found on this page.")
        mem = (msg + (f" AUTO-FILLED from the résumé (do NOT re-fill these): "
                      f"{', '.join(autofilled)[:250]}." if autofilled else ""))
        log.info("upload_resume: %s", mem)
        _emit(pk, "response", agent="browser", url=url, detail=f"📎 {mem}"[:240])
        return ActionResult(extracted_content=msg, long_term_memory=mem[:600])

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
