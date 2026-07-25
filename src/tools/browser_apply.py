"""Actual applying: deterministic Playwright primitives + the submit path.

ADK orchestrates (find → score → tailor → gate/resume); this module drives the
real browser. Two engines share ONE form-filling core (tools.form_fill):

  - "scripted" (default): purely deterministic — banked owner answers, standard
    consents, no model anywhere in the click loop. Unknowns become a gate.
  - "loop": the same core with a model-backed resolver that maps the labels the
    deterministic pass couldn't, and drafts free text via the writer.

The safety policy — never type a placeholder, never affirm a protected status,
always the safe sanctions answer, never submit a job the tracking row already
shows as applied — is enforced in the tool layer here, not in a prompt.
"""

from __future__ import annotations

import contextvars
import json
import re

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

def _headless() -> bool:
    """Run the apply browser headless? Runtime flag (dashboard toggle) wins over
    the env default. Headless = no visible Chrome windows."""
    try:
        from core import flags
        return flags.browser_headless()
    except Exception:
        from core.config import get_settings
        return bool(getattr(get_settings(), "browser_headless", False))


def _is_text_only(model: str) -> bool:
    """True for models with NO vision — we send them no screenshots (image input
    404s them). The Kimi K2 line is text-only; Kimi K2.5+/K3 are multimodal, so
    they keep vision ON (better for dropdown/CAPTCHA-aware form filling)."""
    m = (model or "").lower()
    if "kimi-k2" in m and not any(v in m for v in ("k2.5", "k2.6", "k2.7")):
        return True
    return any(x in m for x in ("qwen2.5-coder", "deepseek-chat", "deepseek-v3",
                                "-instruct-text", "kimi-dev", "moonlight"))


# The tracking statuses that mean "this job already went out under the owner's
# name" — by the pipeline or by their own hand. Submitting again is the one
# mistake this system must never make twice.
_APPLIED_STATUSES = ("applied", "applied_manual")


def _tracking_status(pk: str) -> str:
    """The tracking row's current status for pk, '' when unavailable.

    Best-effort by design: the callers in agent.run checked the row moments ago
    with the same store, so a transient store error here must not kill an apply
    — the portal-side "already applied" check still stands behind it.
    """
    if not pk:
        return ""
    try:
        from core.stores import make_stores
        return str((make_stores().tracking.get(pk) or {}).get("status") or "")
    except Exception:  # noqa: BLE001
        log.debug("could not read tracking row for %s", pk, exc_info=True)
        return ""


def _duplicate_refusal(pk: str, url: str = "") -> dict | None:
    """Refuse loudly when the tracking row already shows this job as applied.

    This is the guard for the incident that motivated it: a job the owner had
    applied to by hand was submitted again by the pipeline. A duplicate
    application under a real person's name is worse than a missed one, so the
    check sits in the tool layer where every engine passes through it — and it
    runs again immediately before every submit click.
    """
    status = _tracking_status(pk)
    if status in _APPLIED_STATUSES:
        detail = (f"REFUSED: this job is already marked '{status}' in tracking — "
                  "not submitting a duplicate application.")
        log.warning("duplicate guard: %s (pk=%s)", detail, pk)
        _emit(pk, "response", agent="applier", url=url, detail=f"🛑 {detail}")
        return {"status": "failed", "reason": "duplicate_application", "detail": detail}
    return None


async def apply(url: str, company: str, facts: dict, model: str, *, pk: str = "",
                jd_text: str = "", resume_tex: str = "", github: str = "",
                resume_path: str = "") -> dict:
    """Fill and SUBMIT this application. Returns one of:
      {status:'applied', confirmation}   {status:'gate', reason, question}
      {status:'failed', reason, detail}  {status:'unknown', detail}

    Engine (config apply_engine): "scripted" (default) = the deterministic
    form-fill core, no LLM in the click loop; "loop" = the same core with a
    model-backed resolver for labels the deterministic pass can't map. Either
    way the outcome shape is identical."""
    from core.config import get_settings

    if dup := _duplicate_refusal(pk, url):
        return dup

    facts = dict(facts)
    full = (facts.get("Full name") or facts.get("Name") or "").strip()
    if full:
        first, *rest = full.split()
        facts.setdefault("First name", first)
        facts.setdefault("Last name", " ".join(rest) or first)

    engine = (getattr(get_settings(), "apply_engine", "scripted") or "scripted").lower()
    if engine == "chrome":
        # The owner's own browser, with their own sessions — the default, because
        # a Playwright browser is what portals challenge. Anyone without Claude
        # Code still gets a working pipeline: this quietly becomes scripted.
        from .claude_chrome import apply_chrome
        from .claude_chrome import available as _chrome_ready

        ready, _why = _chrome_ready()
        if ready:
            return await apply_chrome(url, company, facts, model, pk=pk,
                                      jd_text=jd_text, resume_tex=resume_tex,
                                      github=github, resume_path=resume_path)
        log.info("chrome engine unavailable — using the scripted path")
        engine = "scripted"
    if engine not in ("scripted",):
        log.warning("apply_engine=%r is gone — using the scripted engine", engine)

    async def _hand_over(why: str) -> dict | None:
        """Give the application to the owner's own Chrome.

        The deterministic path fails in two ways that no amount of engine work
        fixes, because neither is about the form: a portal challenges the
        automated browser, or the page is built in a way our reader cannot see.
        The owner's Chrome has their sessions and their history, so it is not
        challenged the same way — that is the whole reason to escalate to it
        rather than retrying the same browser harder.
        """
        from .claude_chrome import apply_chrome, available

        ok, _ = available()
        if not ok:
            return None
        _emit(pk, "response", agent="applier", url=url,
              detail=f"🌐 {why} — handing this to your own Chrome")
        return await apply_chrome(url, company, facts, model, pk=pk, jd_text=jd_text,
                                  resume_tex=resume_tex, github=github,
                                  resume_path=resume_path)

    try:
        result = await _scripted_apply(url, company, facts, model, pk=pk,
                                       jd_text=jd_text, resume_tex=resume_tex,
                                       github=github, resume_path=resume_path)
        # A challenge is the case Chrome exists for; an unreadable page is the
        # other. A gate is NOT escalated: that is the owner being asked something
        # only they can answer, and asking a second agent will not answer it.
        if result and result.get("reason") in ("spam_flagged", "captcha", "login_wall"):
            return await _hand_over("the portal challenged the automated browser") or result
        if result is not None:
            return result
        return (await _hand_over("no readable form on the page")
                or {"status": "unknown",
                    "detail": "No fillable application form was found on the page."})
    except Exception as exc:
        # If the browser was CLOSED or CRASHED, the form was already opened,
        # filled, and (usually) handed to you at the CAPTCHA — re-running could
        # DOUBLE-SUBMIT. Never do that; report uncertain so you verify the portal.
        if _page_gone(exc):
            log.warning("apply: browser closed/crashed (%s) — NOT re-running", exc)
            _emit(pk, "response", agent="applier", url=url,
                  detail="⚠️ the browser closed before I could confirm — I did NOT resubmit. "
                         "Check the portal; if it went through, mark it applied.")
            return {"status": "uncertain",
                    "detail": "Browser closed/crashed during or after submit — not resubmitted; "
                              "verify on the portal whether the application went through."}
        log.exception("apply errored")
        return {"status": "unknown", "detail": f"apply errored: {exc}"}


def _page_gone(exc: Exception) -> bool:
    """True when an error means the page/context/browser was closed or crashed —
    i.e. we can no longer act on it and must NOT restart the whole apply."""
    s = str(exc).lower()
    return any(x in s for x in ("has been closed", "target page", "target closed",
                                "page crashed", "browser has been closed",
                                "context or browser has been closed", "connection closed"))


def _site_rules(url: str, company: str) -> str:
    """Custom instructions for this site, if we've learned any. Never raises —
    a bad note must not stop an application."""
    try:
        from tools.company_skills import instructions_for

        return instructions_for(url, company)
    except Exception:  # noqa: BLE001
        log.debug("could not load company skills", exc_info=True)
        return ""


# --- scripted apply: code drives, the model only maps + writes -----------------

# A browser ERROR page — DNS failure, timeout, connection refused, blank tab.
# This matters because the structural "the form is gone" test cannot tell a
# confirmation screen from a page that never loaded: an error page has no submit
# control, no text inputs and a short body, so a DNS failure was being recorded
# as a submitted application. Positive proof of failure must veto that inference.
_ERROR_PAGE_RX = re.compile(
    r"DNS_PROBE|ERR_[A-Z_]+|NXDOMAIN"
    r"|this site can[’']?t be reached|can[’']?t reach this page"
    r"|took too long to respond|refused to connect|no internet"
    r"|server (?:ip address )?could not be found"
    r"|site cannot be reached|502 bad gateway|503 service unavailable"
    r"|504 gateway timeout|page (?:not found|isn[’']?t working)|http error 4\d\d",
    re.I)


def _is_error_page(url: str, body: str) -> bool:
    """True when the browser is showing a failure, not a page from the site."""
    u = (url or "").lower()
    if u.startswith(("chrome-error://", "about:blank", "chrome://network-error")):
        return True
    return bool(_ERROR_PAGE_RX.search(body or ""))


_SUCCESS_RX = (
    r"thank you for applying|thanks for applying|thank you for your (application|interest)"
    r"|application[\w\s,'\-]{0,60}?(submitted|received|complete|sent|on its way|in review)"
    r"|submitted successfully|successfully (applied|submitted)"
    r"|we('|’)?(ve| have) received your application|received your application"
    r"|your application (is|has been|was)\b")

# The portal saying THIS job was already applied to. This is a DUPLICATE, never
# a retry case: resubmitting — with the primary identity or a fallback one —
# would file a second application under the owner's name. It hard-fails.
_ALREADY_RX = (
    r"you'?ve already applied|you have already applied"
    r"|already applied (?:to|for) this (job|role|position|posting)"
    r"|you have already submitted|already submitted an application (to|for)"
    r"|your application (?:for this (?:job|role|position) )?(?:has|was) already")

# A portal rejecting the submit because the PRIMARY identity hit a RATE cap
# ("You have reached your application limit"). This is a server-side block, not
# a fillable-field error — the owner asked that we re-try such cases with a
# pre-approved SECOND profile (alternate email + phone). Deliberately does NOT
# include "already applied" wording: that is the duplicate case above.
_LIMIT_RX = (
    # ONLY genuine "you are blocked" phrasing — NOT descriptive page notices like
    # "This job has application limits" or "maximum N applications per day" (those
    # falsely tripped the fallback-profile resubmit).
    r"reached your application limit|reached the application limit"
    r"|application limit (has been |was )?reached|application limit reached"
    r"|you have reached the maximum|you'?ve reached the maximum"
    r"|exceeded (the |your )?(maximum|application)")


def _portal_says_already_applied(body: str) -> str | None:
    """The line where the portal states this job was ALREADY applied to, else None.

    Works line-by-line and skips interrogatives, because "Have you already
    applied to this position?" is a legitimate FORM QUESTION on some boards —
    refusing to submit over the question's own wording would block real applies.
    """
    import re as _re

    for ln in (body or "").splitlines():
        line = ln.strip()
        if not line or not _re.search(_ALREADY_RX, line, _re.I):
            continue
        if line.endswith("?") or _re.match(r"(have|has|did|do|were|are)\b", line, _re.I):
            continue  # a question about the past, not a statement of a duplicate
        return line[:180]
    return None

# Lever's anti-bot tripwire: "Your application submission was flagged as possible
# spam. If you believe this was a mistake, please submit your application again."
# A plain resubmit clears it (the owner confirmed clicking retry works) — so we
# retry within the attempt budget instead of gating.
_SPAM_RX = r"flagged as (possible |potential )?spam|please submit your application again"


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
    await _fill_human(page, ident)  # label-match overwrites even autofilled fields
    await page.wait_for_timeout(600)
    return True


async def _scripted_apply(url: str, company: str, facts: dict, model: str, *,
                          pk: str = "", jd_text: str = "", resume_tex: str = "",
                          github: str = "", resume_path: str = "") -> dict | None:
    """The deterministic engine: the shared form-fill core with NO resolver.

    Opens the posting, reaches the form (tab / Apply click / iframe), uploads
    the résumé, runs `fill_form` (banked answers + consent defaults, every write
    verified in the DOM), gates on anything still unanswered, and hands the
    completed form to `_finalize_submit`. Returns None when the page holds no
    fillable form at all.
    """
    import base64

    from playwright.async_api import async_playwright

    from .form_fill import fill_form, read_form

    upload_path = _clean_resume_copy(
        resume_path, facts.get("Full name") or facts.get("Name") or "")

    async with async_playwright() as p:
        page, closer, headed = await _launch(p)
        try:
            _emit(pk, "response", agent="browser", url=url, detail="▶ scripted apply: opening form")
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1500)
            await _reach_form(page)

            # Some employers EMBED the ATS form in an iframe, leaving the
            # top-level document with only the site's nav/search boxes. Work
            # inside whichever frame actually holds the application; `frame`
            # is the page itself when the form is hosted directly.
            frame = await _form_frame(page)
            body = (await frame.evaluate("() => document.body.innerText || ''"))[:6000]
            if _is_error_page(page.url, body):
                return {"status": "unknown",
                        "detail": f"The posting did not load ({page.url})."}

            # An embedded form is served by the ATS itself — detect from the
            # FRAME so a Greenhouse embed gets the Greenhouse skill.
            from .ats import detect_ats
            form_url = (getattr(frame, "url", "") or "") if frame is not page else url
            ats = detect_ats(form_url) or detect_ats(url)

            fields = await read_form(frame, facts, ats)
            fillable = [f for f in fields if f.get("type") not in ("submit", "button")]
            if len([f for f in fillable if f.get("type") not in ("file",)]) < 2:
                return None  # no real form here

            if any(f.get("type") == "password" for f in fillable) and not any(
                    "login" in k.lower() for k in facts):
                shot = base64.b64encode(await page.screenshot()).decode()
                return {"status": "gate", "reason": "no_account", "screenshot_b64": shot,
                        "question": "The portal wants a login before the application form."}

            if upload_path:
                n = await _set_resume_on_page(frame, upload_path)
                _emit(pk, "response", agent="browser", url=url,
                      detail=f"📎 résumé attached to {n} field(s)")
                await page.wait_for_timeout(1500)  # Ashby autofills name/email async

            report = await fill_form(page, frame, facts, resolve=None, ats=ats,
                                     pk=pk, url=url)

            # A missing answer is NOT a reason to leave the rest blank — the
            # form is now as complete as the bank allows, so the owner meets a
            # form that needs one field rather than an empty page.
            if report.unanswered:
                labels = [str(f.get("label") or "")[:60] for f in report.unanswered]
                shot = base64.b64encode(await page.screenshot()).decode()
                _emit(pk, "response", agent="browser", url=url,
                      detail=f"⏸ filled {len(report.filled)} field(s) — over to you "
                             f"for {len(labels)} more")
                return {"status": "gate", "reason": "unknown_field",
                        "screenshot_b64": shot,
                        "question": "The form is filled apart from these — what should "
                                    "they say? " + "; ".join(labels[:5]),
                        "fields": _ui_fields(report.state)}

            result = await _finalize_submit(frame, facts, {}, pk, url, headed,
                                            model=model, shot_page=page)
            return await _maybe_assist(result, page, frame, pk, url, headed)
        finally:
            await closer()


async def _maybe_assist(res: dict, page, frame, pk: str, url: str,  # noqa: ANN001
                        headed: bool) -> dict:
    """Hold the VISIBLE window open for the human ONLY when it's a genuine
    MUST — a CAPTCHA to solve or a login/2FA wall to sign in, or a field only
    they can set. Everything else returns as-is with its screenshot, to be
    reviewed async on the 'Unable to do it' board."""
    import base64

    from core.config import get_settings

    if not (headed and get_settings().assist_captcha):
        return res
    if res.get("status") != "gate" or res.get("reason") not in (
            "captcha", "no_account", "unknown_field"):
        return res
    conf = await _assist_wait(page, pk, url, res.get("question", ""),
                              res.get("reason", ""))
    if not conf:
        return res
    st = await frame.evaluate(_READ_FORM_JS)
    return {"status": "applied", "confirmation": conf,
            "screenshot_b64": base64.b64encode(await page.screenshot()).decode(),
            "drafted": res.get("drafted"), "fields": _ui_fields(st)}


async def _launch(p):  # noqa: ANN001, ANN201
    """Launch the apply browser. Prefers REAL Google Chrome + a persistent profile
    (real fingerprint + accumulated cookies → far fewer CAPTCHAs, and reusable
    portal logins); falls back to bundled Chromium. Returns (page, close, headed)."""
    from pathlib import Path

    from core.config import get_settings

    s = get_settings()
    headless = _headless()
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


async def _confirm_applied_visually(page, model: str) -> bool:  # noqa: ANN001
    """Read the CURRENT page with a vision model and answer one question: does it
    show a job-application confirmation (a thank-you / 'application submitted' /
    'we received your application' screen)? This is the authoritative tie-breaker
    for the Ashby case where the hCaptcha iframe lingers in the DOM after a
    successful submit. Best-effort: any error (no vision model, quota) → False,
    and the caller falls back to text/structural signals."""
    import asyncio  # was missing — every vision confirm silently NameError'd
    import base64

    from litellm import completion

    try:
        shot = base64.b64encode(await page.screenshot()).decode()
    except Exception:
        return False
    # Route to a multimodal model: the configured browser model if it takes
    # images, else a small multimodal OpenAI default. Text-only models are skipped.
    vmodel = model
    if _is_text_only(model):
        vmodel = "openai/gpt-5-mini"
    try:
        resp = await asyncio.to_thread(
            completion, model=vmodel, max_tokens=6,
            messages=[{"role": "user", "content": [
                {"type": "text", "text":
                    "This is a job-application page. Does it VISIBLY show a submission "
                    "confirmation — a 'thank you for applying', 'application submitted', "
                    "'we received your application', or similar success message (NOT just "
                    "an empty form or a CAPTCHA)? Answer with one word: YES or NO."},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{shot}"}}]}])
        ans = (resp["choices"][0]["message"]["content"] or "").strip().upper()
        return ans.startswith("YES")
    except Exception as exc:
        log.info("visual apply-confirmation unavailable (%s) — using text/structural", exc)
        return False


async def _applied_signal(page, url_before: str, body: str | None = None,  # noqa: ANN001
                          model: str = "", allow_vision: bool = False) -> str | None:
    """Return a confirmation string if the page shows the application went
    through, else None. Three signals, cheapest first: (1) success text, (2) a
    confirmation URL, (3) STRUCTURAL — after a submit the form's submit control
    AND the fields are gone, i.e. the form was replaced by a confirmation. Only
    when all three are inconclusive AND vision is allowed do we spend a vision
    call. Deliberately conservative: never claims success on the mere ABSENCE of
    a captcha."""
    import re as _re

    if body is None:
        try:
            body = (await page.evaluate("() => document.body.innerText || ''"))[:6000]
        except Exception:
            body = ""
    m = _re.search(_SUCCESS_RX, body, _re.I)
    if m and not await _form_still_awaiting_input(page):
        return (next((ln.strip() for ln in body.splitlines()
                      if m.group(0).lower()[:16] in ln.lower()), None) or m.group(0))[:120]
    if m:
        # Success wording AND a live form with required fields still unanswered
        # cannot both be true. Something else on the page said "thank you for
        # applying" — a footer, a sibling posting, a pre-rendered panel. Refuse to
        # record it; an uncertain apply the owner reviews is recoverable, a job
        # falsely marked applied is never revisited.
        log.warning("success wording found but the form is still awaiting input "
                    "— not recording this as applied")
    if page.url != url_before and _re.search(
            r"confirmation|submitted|thank|success|application-complete|/complete",
            page.url, _re.I):
        return "Application submitted (confirmation page)."
    # Structural: the form is GONE (no submit control, no text inputs) and the
    # page got short — a confirmation screen, not the filled-in form.
    try:
        has_submit = await page.locator(
            'button:has-text("Submit"), button:has-text("Apply"), input[type=submit]').count()
        n_inputs = await page.locator(
            'input[type=text], input[type=email], input[type=tel], textarea').count()
        if (not has_submit and n_inputs == 0 and body and len(body) < 1800
                and not _is_error_page(page.url, body)):
            return "Application submitted (form replaced by a confirmation page)."
    except Exception:
        pass
    if allow_vision and model and await _confirm_applied_visually(page, model):
        return "Application submitted (visually confirmed on the page)."
    return None


async def _form_still_awaiting_input(page) -> bool:  # noqa: ANN001
    """True when the page is STILL showing a live application form with required
    fields left unanswered.

    Used to veto a success claim. A submitted application does not leave you
    looking at an empty required dropdown, so if both appear to be true the
    success wording came from somewhere other than a confirmation. Returns False
    on any error — this only ever downgrades a claim, never invents one.
    """
    try:
        has_submit = await page.locator(
            'button:has-text("Submit"), button:has-text("Apply"), input[type=submit]').count()
        if not has_submit:
            return False
        empty_required = await page.evaluate("""
        () => [...document.querySelectorAll(
                 'input:not([type=hidden]):not([type=submit]):not([type=button])'
                 + ', textarea, select')]
              .filter(el => (el.required || el.getAttribute('aria-required') === 'true')
                            && !el.value && el.type !== 'checkbox' && el.type !== 'radio')
              .length
        """)
        return bool(empty_required)
    except Exception:  # noqa: BLE001
        return False


async def _assist_wait(page, pk: str, url: str, note: str = "",  # noqa: ANN001
                       reason: str = "", model: str = "") -> str | None:
    """Human handoff: the form is filled and visible — wait for the person to finish
    whatever's actually blocking (a stubborn field, or a CAPTCHA if one is present)
    and click Submit, then return the real confirmation text. The message states the
    REAL blocker — it never claims a CAPTCHA when the blocker is a field."""

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
    for _tick in range(max(1, secs // 3)):
        try:
            await page.wait_for_timeout(3000)
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
        # Text/url/structural every tick; a vision check every ~30s so a
        # confirmation the wording missed still auto-completes.
        conf = await _applied_signal(page, url, model=model,
                                     allow_vision=(model and _tick % 10 == 9))
        if conf:
            line = conf
            _emit(pk, "response", agent="applier", url=url,
                  detail=f"✅ you submitted it — confirmed: {line[:80]}")
            return line[:120]
    return None


# EEO / self-identification options. These declare something about the owner that
# only they can declare — a disability, veteran status, an ethnicity. They are
# also VOLUNTARY, so the safe answer is always the negative or the decline, never
# the affirmative. An option is not evidence of an answer: "Yes, I have a
# disability, or have had one in the past" is a sentence the form offers, not
# something the pipeline may tick on the owner's behalf.
_SELF_ID_RX = re.compile(
    r"disabilit|protected veteran|veteran status|hispanic|latino"
    r"|race|ethnicit|gender identity|transgender|sexual orientation", re.I)
_SELF_ID_AFFIRM_RX = re.compile(
    r"^\s*yes\b|^\s*i (identify|have|am)\b|i identify as one or more", re.I)


# "I am NOT a protected veteran" and "No, I do not have a disability" are the
# NEGATIVE answers to the same questions, and refusing those would leave a
# required EEO field blank instead of correctly declined.
_SELF_ID_NEGATION_RX = re.compile(
    r"\bnot?\b|\bdon'?t\b|\bdo not\b|\bnone\b|decline|wish to answer", re.I)


def _is_self_id_affirmation(label: str, value: object = "") -> bool:
    """True when ticking this would DECLARE a protected characteristic."""
    text = f"{label} {value}".strip()
    head = str(label).strip()
    if _SELF_ID_NEGATION_RX.search(head):
        return False
    return bool(_SELF_ID_RX.search(text) and _SELF_ID_AFFIRM_RX.search(head))


def _consent_default(label: str, options: list | None) -> str | None:
    """Standard job-application CONSENTS — 'I certify the info is true', 'I
    understand the privacy policy', 'I authorize consideration for other roles',
    'I acknowledge…' — are always affirmed by an applicant who is submitting.
    If the label is clearly such a consent AND the field offers an affirmative
    option (Yes / I agree / I accept), return it; else None. Deliberately
    conservative: it NEVER fires for a substantive question (visa, relocation,
    clearance) or for a waiver / fee / arbitration clause, which must reach the
    human."""
    import re as _re

    text = (label or "").lower()
    consent = _re.search(
        r"\bi\s+(certify|understand|acknowledge|authorize|agree|consent|confirm)\b"
        r"|privacy (policy|notice|statement)|terms (and|&) conditions"
        r"|processed in accordance|candidate privacy", text)
    if not consent:
        return None
    if _re.search(r"waive|waiver|\bfee\b|payment|arbitrat|background check fee", text):
        return None  # never auto-affirm a waiver / fee — that's the human's call
    aff = _re.compile(
        r"^(yes\b|agree\b|accept\b|i\s+(agree|certify|understand|authorize|acknowledge"
        r"|consent|accept|confirm)\b|i do\b|true\b)", _re.I)
    for o in (options or []):
        if aff.match((o or "").strip()):
            return o
    return None


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
            await _bring_into_view(el)
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
                read = await el.evaluate("e => (e.value || e.textContent || '').trim()")
                got = (read or "").lower()
                city = value.split(",")[0].strip().lower()
                if got and (typed.lower() in got or city in got):
                    _emit(pk, "response", agent="browser", url=url,
                          detail=f"📍 selected: {got[:40]}")
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
                await _fill_human(page, {k: v})
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


# Scores a document on how much it looks like a JOB APPLICATION form rather than
# a page that merely has inputs. A résumé file input is the strongest signal, an
# email field next; a plain count is not enough, because a careers page carries a
# nav search box and a cookie form while the real application sits in an iframe.
_FORM_SCORE_JS = """
() => {
  const q = s => document.querySelectorAll(s).length;
  const files = q('input[type=file]');
  const emails = q('input[type=email]');
  const texts = q('input[type=text], input[type=tel], textarea, select');
  return files * 100 + emails * 25 + texts;
}
"""


async def _reach_form(page) -> bool:  # noqa: ANN001
    """Click through to the actual application form. Best-effort, never raises.

    A job page is not an application: Ashby keeps the fields behind an
    "Application" tab and reading the posting itself finds none at all, while
    Greenhouse and Lever variants use an Apply link or button. Shared by every
    engine so they cannot disagree about where the form is.
    """
    for sel in ('role=tab[name*="Application"]', 'text="Application"',
                'a:has-text("Apply for this job")', 'a:has-text("Apply now")',
                'button:has-text("Apply")'):
        try:
            await page.locator(sel).first.click(timeout=2500)
            await page.wait_for_timeout(1200)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def _form_frame(page):  # noqa: ANN001, ANN201
    """The frame that actually holds the application form.

    Employers commonly EMBED the ATS form in an iframe (a Greenhouse embed is the
    usual case), which leaves the top-level document with no résumé input and only
    the site's own nav/search boxes. Reading, filling or uploading against the top
    level then silently does nothing — the fill reports NOT FOUND for every field
    and the résumé is never attached, while the page still looks right on screen.

    Returns the top-level page unless a child frame scores strictly higher, so
    sites that host their form directly (Ashby, Lever) are unaffected.
    """
    try:
        best, best_score = page, await page.evaluate(_FORM_SCORE_JS)
        for frame in getattr(page, "frames", []) or []:
            if frame is getattr(page, "main_frame", None):
                continue
            try:
                score = await frame.evaluate(_FORM_SCORE_JS)
            except Exception:  # noqa: BLE001 — a cross-origin/detached frame
                continue
            if score > best_score:
                best, best_score = frame, score
        if best is not page:
            log.info("application form is in an iframe (%s) — working inside it",
                     (getattr(best, "url", "") or "")[:80])
        return best
    except Exception:  # noqa: BLE001 — never lose an apply to frame detection
        log.debug("frame detection failed; using the top-level page", exc_info=True)
        return page


_READ_FORM_JS = """
() => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  // COOKIE/consent banners are not part of the application. They sit in every
  // page, carry checkbox-shaped controls ("Performance Cookies", "Targeting
  // Cookies"), and an agent that can see them will happily toggle them — which
  // both corrupts the answers and changes the owner's privacy settings. Match
  // the banner CONTAINER, never the word "consent" alone: real applications ask
  // genuine consent questions that must still be answered.
  const inCookieBanner = el => !!(el.closest && el.closest(
    '#onetrust-consent-sdk, #onetrust-banner-sdk, #onetrust-pc-sdk, .ot-sdk-container,'
    + ' .optanon-alert-box-wrapper, #CybotCookiebotDialog, .cc-window, .cookie-banner,'
    + ' [id*="cookie" i], [class*="cookie-banner" i], [class*="cookieBanner" i],'
    + ' [aria-label*="cookie" i], [data-testid*="cookie" i]'));
  const els = [...document.querySelectorAll('input, textarea, select')]
    .filter(el => el.type !== 'hidden' && !inCookieBanner(el));
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
  // A form marks "required" for a PERSON, and that is usually an asterisk in the
  // label rather than an HTML attribute. OpenAI's Ashby board marks its location
  // combobox and its arbitration acknowledgement with a red * and sets neither
  // `required` nor aria-required — so reading attributes alone told every engine
  // those fields were optional, they were left blank, and the submit failed on
  // them. Look for the asterisk in the label's own element too.
  // Two shapes are mandatory in practice on every board, whatever the markup
  // says: where you are located, and the acknowledgement you must tick to file
  // an application. Encoding the SHAPE rather than listing fields means a board
  // we have never seen is handled the first time, and there is nothing to keep
  // in sync as employers reword their forms.
  const ALWAYS_REQUIRED = new RegExp([
    'where are you (currently )?located', 'current location', '^location$',
    'i acknowledge', 'i confirm', 'i certify', 'i have read',
    'arbitration agreement', 'consent to',
  ].join('|'), 'i');
  const isRequired = (el, label) => {
    if (el.required || el.getAttribute('aria-required') === 'true') return true;
    if (label && ALWAYS_REQUIRED.test(label)) return true;
    const owners = [el.labels && el.labels[0],
                    el.closest('[class*="fieldEntry" i], [class*="field" i], fieldset, label')];
    for (const o of owners) {
      if (!o) continue;
      const head = o.querySelector('label, legend, .heading, [class*="heading" i]') || o;
      if (/\\*/.test((head.textContent || '').slice(0, 200))) return true;
      if (o.querySelector('[class*="required" i], .asterisk')) return true;
    }
    return false;
  };
  const out = [], groups = {};
  for (const el of els) {
    const multi = el.name &&
      document.querySelectorAll('input[name="' + CSS.escape(el.name) + '"]').length > 1;
    if (el.type === 'radio' || (el.type === 'checkbox' && multi)) {
      const key = el.name || optLabel(el);
      if (!groups[key]) groups[key] = {
        label: questionOf(el) || key, type: 'choice-group', options: [], value: '',
        required: isRequired(el, questionOf(el) || key) };
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
    if (!lbl) {  // the field wrapper's OWN unbound <label> (Ashby: 'Location',
      // 'Name' — a direct child of the field container, not tied by id). This
      // beats questionOf, which climbs to the SECTION heading for a short label.
      for (let fw = el.parentElement, i = 0; i < 4 && fw && !lbl; i++, fw = fw.parentElement) {
        const own = [...fw.children].find(c => c.tagName === 'LABEL');
        if (own) { const t = norm(own.textContent); if (t && t.length < 60) lbl = t; }
      }
    }
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
      required: isRequired(el, lbl),
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
(payload) => {
  // payload is either a bare {label: value} map (legacy, fills from JS) or
  // {map, opts:{tagOnly}}. In tagOnly mode this NEVER writes a value: it only
  // MARKS the matched control with data-ai-fill and returns a plan, so Playwright
  // can drive real clicks + keystrokes. JS-set values dispatch synthetic events
  // (isTrusted:false, no keydown, 40 chars in 0ms) — the exact fingerprint ATS
  // spam filters reject with "your submission was flagged as possible spam".
  const map = (payload && payload.map) ? payload.map : payload;
  const tagOnly = !!(payload && payload.opts && payload.opts.tagOnly);
  const norm = s => (s || '').toLowerCase().replace(/\\s+/g, ' ').trim();
  const ntrim = s => (s || '').replace(/\\s+/g, ' ').trim();
  const setVal = (el, v) => {
    const proto = el.tagName === 'TEXTAREA'
      ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  // COOKIE/consent banners are not part of the application. They sit in every
  // page, carry checkbox-shaped controls ("Performance Cookies", "Targeting
  // Cookies"), and an agent that can see them will happily toggle them — which
  // both corrupts the answers and changes the owner's privacy settings. Match
  // the banner CONTAINER, never the word "consent" alone: real applications ask
  // genuine consent questions that must still be answered.
  const inCookieBanner = el => !!(el.closest && el.closest(
    '#onetrust-consent-sdk, #onetrust-banner-sdk, #onetrust-pc-sdk, .ot-sdk-container,'
    + ' .optanon-alert-box-wrapper, #CybotCookiebotDialog, .cc-window, .cookie-banner,'
    + ' [id*="cookie" i], [class*="cookie-banner" i], [class*="cookieBanner" i],'
    + ' [aria-label*="cookie" i], [data-testid*="cookie" i]'));
  const ctrls = [...document.querySelectorAll('input, textarea, select')]
    .filter(e => e.type !== 'hidden' && !inCookieBanner(e));
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
    // Field wrapper's OWN unbound <label> (Ashby: Location, etc.) — a direct
    // child of the field container — before questionOf climbs to the section.
    for (let fw = el.parentElement, i = 0; i < 4 && fw; i++, fw = fw.parentElement) {
      const own = [...fw.children].find(c => c.tagName === 'LABEL');
      if (own) { const t = ntrim(own.textContent); if (t && t.length < 60) return t; }
    }
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
  const plan = [];
  let tagN = 0;
  // Perform the edit (legacy) or mark the control for Playwright (tagOnly).
  const act = (el, key, kind, val, optValue) => {
    if (tagOnly) {
      const token = 'f' + (++tagN);
      el.setAttribute('data-ai-fill', token);
      plan.push({ key: key, token: token, kind: kind,
                  value: String(val), option: optValue || '' });
      return true;
    }
    if (kind === 'select') {
      el.value = optValue;
      el.dispatchEvent(new Event('change', { bubbles: true }));
    } else if (kind === 'check') { el.click(); }
    else { setVal(el, val); }
    return true;
  };
  for (const [key, val] of Object.entries(map)) {
    const k = norm(key);
    if (!k || !val) { report.push('SKIPPED (empty): ' + key); continue; }
    let done = false;
    // 1) text / textarea / select, matched by label. Take the BEST match, not
    // the first: "First Name" is a substring of "Preferred First Name", so
    // first-match-wins let one key consume the other field and left a required
    // one empty. Exact label wins; otherwise the closest-length containment.
    {
      let pick = null, pickCost = Infinity;
      for (const el of ctrls) {
        if (!isText(el)) continue;
        if (el.hasAttribute('data-ai-fill')) continue;   // already claimed this pass
        const l = norm(labelOf(el));
        if (!l) continue;
        let cost = null;
        if (l === k) cost = 0;
        else if (l.includes(k) || k.includes(l)) cost = Math.abs(l.length - k.length) + 1;
        if (cost !== null && cost < pickCost) { pick = el; pickCost = cost; }
      }
      if (pick) {
        if (pick.tagName === 'SELECT') {
          const opt = [...pick.options].find(o => norm(o.textContent).includes(norm(val)));
          if (opt) { done = act(pick, key, 'select', val, opt.value); }
        } else { done = act(pick, key, 'text', val); }
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
          if (m) { done = act(m, key, 'check', val); }
          else if (boxes.length === 1 &&
                   /^(yes|true|checked|agree|accept)/.test(norm(val))) {
            done = act(boxes[0], key, 'check', val);  // lone consent checkbox
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
        if (best && bestScore >= 0.5 && bestScore > second) {
          done = act(best, key, best.tagName === 'SELECT' ? 'select' : 'text', val,
                     best.tagName === 'SELECT' ? val : '');
        }
      }
    }
    report.push((done ? 'FILLED: ' : 'NOT FOUND: ') + key);
  }
  return tagOnly ? { plan: plan, report: report } : report;
}
"""

# Values that are a TEMPLATE REFERENCE, not an answer. The apply agent is told to
# call draft_essay_answer and reuse its text verbatim; sometimes it instead writes
# the token standing for that text ("{{DRAFT_ESSAY_ANSWER}}"), and a literal
# placeholder then gets typed into a real job application. Nothing matching this
# is ever entered into a form.
_PLACEHOLDER_RX = re.compile(
    r"^\s*(?:\{\{.*\}\}|<[A-Z_ ]{3,}>|\[(?:INSERT|YOUR|TODO|DRAFT)[^\]]*\]"
    r"|(?:DRAFT_ESSAY_ANSWER|YOUR_ANSWER_HERE|ANSWER_HERE|TODO|TBD|N/?A_PLACEHOLDER))\s*$",
    re.IGNORECASE)


def _is_placeholder(value: object) -> bool:
    return isinstance(value, str) and bool(_PLACEHOLDER_RX.match(value))


async def _bring_into_view(target, timeout: int = 2500) -> None:  # noqa: ANN001
    """Scroll an element into view, best-effort.

    Playwright scrolls before a click regardless, so this is only about making
    the right thing visible in a headed window. It must never raise: under load
    the scroll times out long before the click would, and an application was lost
    to exactly that — two answerable radio questions reported as unsettable.
    """
    try:
        await target.scroll_into_view_if_needed(timeout=timeout)
    except Exception:  # noqa: BLE001
        log.debug("scroll_into_view timed out — clicking anyway", exc_info=True)


async def _fill_human(page, mapping: dict) -> list:  # noqa: ANN001
    """Fill a form the way a PERSON does — real mouse clicks, real keystrokes.

    Every edit here goes through Playwright's CDP input pipeline, so the page sees
    TRUSTED events (isTrusted:true) in a genuine focus → keydown → input → keyup →
    blur sequence, at human speed. Filling from JavaScript instead (assigning
    .value and dispatching synthetic events) is what gets applications rejected
    with "your submission was flagged as possible spam": no key events ever fire,
    focus never moves, and forty characters appear in zero milliseconds.

    JS is still used for MATCHING — the label resolvers are subtle and shared with
    the form reader — but it only TAGS the controls; the typing happens here.
    Returns the same FILLED:/NOT FOUND: report the old JS fill returned, so every
    caller keeps working unchanged.
    """
    import random

    # Refuse template tokens BEFORE anything is typed — this is the one choke
    # point every value passes through, so a placeholder can never reach a form.
    blocked = [k for k, v in mapping.items() if _is_placeholder(v)]
    if blocked:
        log.warning("refusing to type placeholder values: %s", ", ".join(blocked))
        mapping = {k: v for k, v in mapping.items() if k not in blocked}
    # Never DECLARE a protected characteristic. These questions are voluntary and
    # the answer is the owner's alone, so an affirmative self-identification is
    # dropped rather than guessed at.
    selfid = [k for k, v in mapping.items() if _is_self_id_affirmation(k, v)]
    if selfid:
        log.warning("refusing to self-declare on the owner's behalf: %s",
                    "; ".join(x[:60] for x in selfid))
        mapping = {k: v for k, v in mapping.items() if k not in selfid}
    if not mapping:
        return [f"PLACEHOLDER (not typed): {k}" for k in blocked]

    try:
        out = await page.evaluate(_FILL_JS, {"map": mapping, "opts": {"tagOnly": True}})
    except Exception:  # noqa: BLE001 — never lose a fill to a resolver change
        log.debug("tagging fill failed; falling back to JS fill", exc_info=True)
        return await page.evaluate(_FILL_JS, mapping)

    plan = (out or {}).get("plan") or []
    report = list((out or {}).get("report") or [])
    failed: set[str] = set()
    try:
        for step in plan:
            key = step.get("key", "")
            value = str(step.get("value") or "")
            kind = step.get("kind")
            loc = page.locator(f'[data-ai-fill="{step.get("token")}"]').first
            try:
                await _bring_into_view(loc)
                if kind == "check":
                    if not await loc.is_checked():
                        await loc.click(timeout=3000)  # real click on the box/label
                elif kind == "select":
                    # <select> has no typing analogue; select_option still drives a
                    # trusted change event through CDP.
                    await loc.select_option(step.get("option") or value, timeout=3000)
                else:
                    await loc.click(timeout=3000)  # focus by clicking, like a person
                    await loc.fill("", timeout=2000)  # trusted clear (CDP, not .value)
                    if len(value) > 250:
                        # Nobody hand-types a cover letter; a paste is honest input
                        # and keeps a 2000-char answer from taking two minutes.
                        await loc.fill(value, timeout=8000)
                    else:
                        await loc.type(value, delay=random.uniform(28, 55),
                                       timeout=max(6000, len(value) * 120))
                    await loc.blur()  # commit: fires the change/validation handlers
                await page.wait_for_timeout(random.uniform(90, 260))  # human cadence
            except Exception:  # noqa: BLE001
                log.debug("human fill failed for %s", key, exc_info=True)
                failed.add(key)
    finally:
        try:
            await page.evaluate(
                "() => document.querySelectorAll('[data-ai-fill]')"
                ".forEach(e => e.removeAttribute('data-ai-fill'))")
        except Exception:  # noqa: BLE001
            log.debug("tag cleanup failed", exc_info=True)

    # A tagged field we could not actually drive is NOT filled — say so, or the
    # caller believes the form is complete and submits a half-empty application.
    return [f"NOT FOUND: {line.split(': ', 1)[1]}"
            if line.startswith("FILLED: ") and line.split(": ", 1)[1] in failed else line
            for line in report] + [f"PLACEHOLDER (not typed): {k}" for k in blocked]


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


async def _click_submit(page, pk: str = "", url: str = "") -> str:  # noqa: ANN001
    """Press the submit control with a REAL trusted mouse click — portals'
    anti-bot heuristics (Lever's 'possible spam') discount synthetic JS clicks,
    so the click that submits must look human. Falls back to the JS click only
    when no visible submit control can be located."""
    import re as _re

    for sel in ('button:has-text("Submit application")',
                'button:has-text("Submit")', 'input[type="submit"]',
                'button:has-text("Apply")'):
        try:
            el = page.locator(sel).first
            txt = ((await el.text_content(timeout=800)) or
                   (await el.get_attribute("value")) or "").strip()
            if _re.search(r"linkedin|indeed|dropbox|google|autofill|with\s", txt, _re.I):
                continue  # third-party "Apply with …" buttons are not the submit
            await _bring_into_view(el)
            await el.click(timeout=2500)
            return txt or "Submit"
        except Exception:
            continue
    try:
        return await page.evaluate(_SUBMIT_JS)
    except Exception:
        return ""


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
                await _bring_into_view(loc)
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


async def _restore_wiped_form(page, state: list, facts: dict, pk: str, url: str) -> int:  # noqa: ANN001
    """Refill every required field the portal just blanked. Returns how many.

    Several portals answer a rejected submit by RELOADING the form — classic
    Lever on a validation error, Ashby on a spam flag — which wipes fields that
    were perfectly good. Resubmitting without restoring them sends an empty
    application, and an empty resubmit looks far more like a bot than the
    submission that got flagged in the first place.
    """
    import asyncio as _aio

    empty = [f for f in state if f.get("required") and not f.get("value")
             and f.get("type") not in ("file",)]
    text_fix, choice_fix, combo_fix = {}, {}, {}
    for f in empty:
        hit = _fact_for(f.get("label", ""), facts)
        if not hit:
            continue
        _, v = hit
        if f.get("type") == "choice-group":
            choice_fix[f["label"]] = v
        elif f.get("combo"):
            combo_fix[f["label"]] = v
        else:
            text_fix[f["label"]] = v
    total = len(text_fix) + len(choice_fix) + len(combo_fix)
    if total < 2:
        return 0
    _emit(pk, "response", agent="browser", url=url,
          detail=f"🔄 the submit reset the form — restoring {total} field(s)")

    async def _restore() -> None:
        if text_fix:
            await _fill_human(page, text_fix)
            await page.wait_for_timeout(700)
        if choice_fix:
            await _set_choices(page, choice_fix, pk, url)
        if combo_fix:
            await _fix_fields(page, combo_fix, pk, url)
        await _ensure_sanctions_safe(page, pk, url)
        await _click_fields_pass(page, pk, url)

    try:
        # HARD ceiling: evaluate() on a page mid-navigation can block forever and
        # wedge the whole apply lane — a timed-out restore just resubmits.
        await _aio.wait_for(_restore(), timeout=150)
    except TimeoutError:
        log.warning("restore after form reset timed out — resubmitting anyway")
    except Exception as exc:  # noqa: BLE001
        if _page_gone(exc):
            raise
        log.warning("restore after form reset failed (%s) — resubmitting", exc)
    return total


_VERIFY_FIELD_JS = r"""
({q}) => {
  const norm = s => (s || '').toLowerCase().replace(/\s+/g, ' ').trim();
  const nq = norm(q);
  for (const el of document.querySelectorAll('input,textarea,select')) {
    const lbl = norm((el.labels && el.labels[0] && el.labels[0].textContent)
                     || el.getAttribute('aria-label') || el.name || '');
    const box = el.closest('[class*="fieldEntry" i], fieldset, label, div');
    const near = norm((box || {}).textContent || '');
    if (!lbl.includes(nq.slice(0, 34)) && !near.includes(nq.slice(0, 34))) continue;
    if (el.type === 'checkbox' || el.type === 'radio') {
      if (el.checked) return 'checked';
      continue;
    }
    if (el.type === 'file') return (el.files && el.files.length) ? el.files[0].name : '';
    if (el.value) return el.value;
  }
  return '';
}
"""


async def set_field(page, label: str, value: object, pk: str = "", url: str = "") -> str:  # noqa: ANN001
    """Set one control to `value`, whatever kind of control it turns out to be.

    Every apply failure this pipeline has had takes the same shape: we decide a
    field's type from what the reader told us, commit to one way of setting it,
    and when that guess is wrong nothing happens at all — an Ashby Yes/No is two
    buttons over a hidden checkbox, a location is a geocoder that ignores typing,
    a consent is a lone box with no option to match. Each was a separate patch.

    So stop guessing. Try each way of setting a control in turn and check the DOM
    after every attempt, stopping at the one that actually took. A board we have
    never seen gets the same treatment as one we have, and a new widget costs a
    strategy here rather than a special case in every caller.
    """
    want = str(value)

    async def _current() -> str:
        try:
            return str(await page.evaluate(_VERIFY_FIELD_JS, {"q": label}) or "")
        except Exception:  # noqa: BLE001
            return ""

    before = await _current()
    if before and (before == want or _norm_txt(before) == _norm_txt(want)
                   or before == "checked"):
        return "already"

    async def _visible_option() -> None:
        await _set_choices(page, {label: want}, pk, url)

    async def _typed() -> None:
        await _fill_human(page, {label: want})

    async def _geocoder() -> None:
        await _fix_fields(page, {label: want.split(",")[0].strip()}, pk, url)

    for name, attempt in (("option", _visible_option), ("type", _typed),
                          ("combobox", _geocoder)):
        try:
            await attempt()
        except Exception as exc:  # noqa: BLE001
            if _page_gone(exc):
                raise
            log.debug("set_field %r via %s raised: %s", label[:40], name, exc)
        await page.wait_for_timeout(400)
        now = await _current()
        if now and now != before:
            log.info("set_field %r via %s", label[:48], name)
            return "set"
    return "failed"


def _norm_txt(v: object) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


async def _finalize_submit(page, facts: dict, drafted: dict, pk: str, url: str,  # noqa: ANN001
                           headed: bool, model: str = "", shot_page=None) -> dict:  # noqa: ANN001
    """Deterministic finish on an already-FILLED page: submit → read the form's
    own errors → self-heal only those → resubmit; on real confirmation return
    applied; on a CAPTCHA / stubborn field, hand the visible window to the human
    and watch for the confirmation. Never touches a CAPTCHA itself.

    `page` may be the FRAME holding an embedded form; `shot_page` is then the
    top-level Page for screenshots (defaults to `page`).

    Before ANY click, and again before every resubmit, the duplicate guard runs:
    a job the tracking row already shows as applied — or a portal page stating
    "you have already applied" — is refused loudly, never submitted."""
    import base64
    import re as _re

    from core.config import get_settings

    shot_page = shot_page if shot_page is not None else page

    # The portal can say the application already exists BEFORE we ever click
    # (a banner on the form page). That is a duplicate — stop here.
    try:
        pre = (await page.evaluate("() => document.body.innerText || ''"))[:6000]
    except Exception:  # noqa: BLE001
        pre = ""
    if line := _portal_says_already_applied(pre):
        _emit(pk, "response", agent="applier", url=url,
              detail="🛑 the portal says this was already applied to — "
                     f"not submitting: {line[:120]}")
        return {"status": "failed", "reason": "duplicate_application",
                "detail": f"The portal reports an existing application: {line} "
                          "Not submitting a duplicate.", "drafted": drafted or None}

    body, shot, swapped = "", None, False
    for attempt in (0, 1, 2):
        # Re-check the tracking row IMMEDIATELY before each click: the owner may
        # have marked the job applied (or applied by hand) while this ran.
        if dup := _duplicate_refusal(pk, url):
            dup["drafted"] = drafted or None
            return dup
        clicked = await _click_submit(page)
        if clicked:
            _emit(pk, "response", agent="browser", url=url, detail=f"⏎ clicked: {clicked}")
        await page.wait_for_timeout(5000)
        body = (await page.evaluate("() => document.body.innerText || ''"))[:6000]
        shot = base64.b64encode(await shot_page.screenshot()).decode()
        state = await page.evaluate(_READ_FORM_JS)
        # "Already applied" BEFORE limit/success: it is a duplicate, and neither
        # a fallback-identity resubmit (that files a second application under an
        # alternate email) nor a success read is ever the right response to it.
        if line := _portal_says_already_applied(body):
            return {"status": "failed", "reason": "duplicate_application",
                    "screenshot_b64": shot,
                    "detail": f"The portal reports an existing application: {line} "
                              "Not resubmitting — a duplicate under your name is "
                              "worse than a missed application.",
                    "drafted": drafted or None, "fields": _ui_fields(state)}
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
        if _re.search(_SPAM_RX, body, _re.I):
            if attempt < 2:
                # The flag RESETS the form ("please submit your application
                # again"), so a straight retry submits an EMPTY one — which reads
                # as far more of a bot than the attempt that got flagged. Restore
                # the answers, then wait: submitting again within milliseconds is
                # itself the signal these heuristics look for.
                _emit(pk, "response", agent="browser", url=url,
                      detail="⚠️ flagged as possible spam — restoring the form and "
                             "waiting before resubmitting")
                await _restore_wiped_form(page, state, facts, pk, url)
                await page.wait_for_timeout(20000 * (attempt + 1))
                continue
            return {"status": "failed", "reason": "spam_flagged",
                    "detail": "The portal flagged the submission as possible spam twice. "
                              "Retry from the Flagged lane re-runs it with human-style "
                              "clicks — waiting a few minutes helps.",
                    "screenshot_b64": shot, "drafted": drafted or None,
                    "fields": _ui_fields(state)}
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
        # A rejected submit can reload the form and wipe good answers; restore
        # them and try again before blaming the fields.
        if attempt < 2 and await _restore_wiped_form(page, state, facts, pk, url):
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
            await _fill_human(page, text_fix)
        if choice_fix:
            await _set_choices(page, choice_fix, pk, url)
        await page.wait_for_timeout(900)

    # Terminal: re-read the LIVE page (the loop's `body` predates the final
    # submit). A visible confirmation is AUTHORITATIVE — Ashby keeps the hCaptcha
    # iframe in the DOM even after a successful submit, so we must never gate
    # "solve the CAPTCHA" over a page that already shows "thank you". Checking
    # success here first is what prevents a double-submit.
    try:
        body = (await page.evaluate("() => document.body.innerText || ''"))[:6000]
    except Exception:
        pass
    # A visible confirmation is AUTHORITATIVE — read the live page (text →
    # url → structural → vision). This is what stops a succeeded application
    # from being false-gated as a CAPTCHA and re-submitted.
    conf = await _applied_signal(page, url, body, model=model, allow_vision=True)
    if conf:
        st = await page.evaluate(_READ_FORM_JS)
        return {"status": "applied", "confirmation": conf,
                "screenshot_b64": base64.b64encode(await shot_page.screenshot()).decode(),
                "drafted": drafted or None, "fields": _ui_fields(st)}

    state = await page.evaluate(_READ_FORM_JS)
    empty_req = [f for f in state if f.get("required") and not f.get("value")
                 and f.get("type") != "file"]
    # A REAL captcha block = the challenge is present AND the submit control is
    # still there (still on the form). A leftover iframe on a confirmation page
    # is not a blocker — and _applied_signal already returned above if applied.
    has_submit = bool(await page.locator(
        'button:has-text("Submit"), input[type=submit]').count())
    captcha = has_submit and (bool(_re.search(r"captcha", body, _re.I)) or bool(
        await page.locator('iframe[src*="recaptcha"], iframe[src*="hcaptcha"]').count()))
    # Only hold the window open for a REAL CAPTCHA (a genuine must). An empty field
    # or an unconfirmed submit falls through to a board gate (with a screenshot) —
    # we don't pull the human into a live window unless one is truly required.
    if headed and get_settings().assist_captcha and captcha:
        conf = await _assist_wait(shot_page, pk, url, "", "captcha", model=model)
        if conf:
            st = await page.evaluate(_READ_FORM_JS)
            return {"status": "applied", "confirmation": conf,
                    "screenshot_b64": base64.b64encode(await shot_page.screenshot()).decode(),
                    "drafted": drafted or None, "fields": _ui_fields(st)}
    if empty_req:
        labels = [f["label"] for f in empty_req]
        # The gate says "in the window" — so actually HOLD the window open (no
        # expiry) and watch for the human to finish, instead of killing it and
        # pointing at a window that no longer exists.
        if headed and get_settings().assist_captcha:
            asked = "; ".join(lab[:60] for lab in labels[:4])
            conf = await _assist_wait(shot_page, pk, url, f"set {asked}", "",
                                      model=model)
            if conf:
                st = await page.evaluate(_READ_FORM_JS)
                return {"status": "applied", "confirmation": conf,
                        "screenshot_b64": base64.b64encode(await shot_page.screenshot()).decode(),
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


# Export-control / sanctions screening questions. The candidate has no such
# citizenship, residency or tie, so the answer is ALWAYS negative. This is not a
# preference: a false affirmative is a self-reported disqualifier that no
# recruiter revisits, and it would be entered under the owner's name. Enforced in
# code rather than left to a label match, because the cost of one wrong click
# here is categorically worse than for any other field on the form.
_SANCTIONS_RX = re.compile(
    r"cuba|iran\b|north korea|syria|crimea|donetsk|luhansk|zaporizhzhia|kherson"
    r"|\bbelarus\b|sanction|embargo|export control|restricted (?:country|party)",
    re.I)
# Option wording that SAFELY answers such a question.
_SAFE_OPTION_RX = _SANCTIONS_SAFE_RX = re.compile(
    r"^\s*(?:no|none of the above|not applicable|n/?a|neither|none)\b", re.I)


def _safe_sanctions_answer(question: str, values: list) -> list:
    """Force a sanctions question to its negative answer.

    Keeps only options that are plainly negative. If the caller proposed an
    affirmative one, it is dropped and 'None of the above' / 'No' is used, so a
    mis-mapped answer cannot tick "citizen of a sanctioned country".
    """
    if not _SANCTIONS_RX.search(question or ""):
        return values
    safe = [v for v in values if _SANCTIONS_SAFE_RX.match(str(v))]
    if safe != list(values):
        log.warning("sanctions question — forcing the negative answer for %r "
                    "(proposed %r)", (question or "")[:70], values)
    return safe or ["None of the above", "No", "Not applicable"]


# Finds a sanctions checkbox/radio group and its SAFE option, wherever it sits.
# Structure varies (fieldset+legend, a bare paragraph then labels, a div), and
# the generic reader does not always group it — so this works off the OPTION
# text, which is consistent, rather than the question's markup.
_SANCTIONS_SWEEP_JS = r"""
() => {
  const ntrim = s => (s || '').replace(/\s+/g, ' ').trim();
  const labelOf = el => ntrim(
       (el.labels && el.labels[0] && el.labels[0].textContent)
    || (el.closest('label') || {}).textContent
    || (el.nextElementSibling || {}).textContent || '');
  const COUNTRY = new RegExp('cuba|iran\\b|north korea|syria|crimea|donetsk'
    + '|luhansk|zaporizhzhia|kherson|\\bbelarus\\b|\\brussia\\b', 'i');
  const SAFE = /^(none of the above|not applicable|none of these apply|no)\b/i;

  const boxes = [...document.querySelectorAll('input[type=checkbox], input[type=radio]')];
  const risky = boxes.filter(b => COUNTRY.test(labelOf(b)));
  if (!risky.length) return null;

  // The group is whatever container holds the risky options.
  let scope = risky[0].parentElement;
  for (let i = 0; i < 6 && scope; i++) {
    if (risky.every(r => scope.contains(r))) break;
    scope = scope.parentElement;
  }
  scope = scope || document.body;
  const inGroup = boxes.filter(b => scope.contains(b));
  const safe = inGroup.find(b => SAFE.test(labelOf(b)));
  const mark = (el, token) => { el.setAttribute('data-ai-sanctions', token); return token; };
  return {
    checkedRisky: risky.filter(b => b.checked).map((b, i) => mark(b, 'risky' + i)),
    safe: safe ? (safe.checked ? 'ALREADY' : mark(safe, 'safe')) : '',
    safeLabel: safe ? labelOf(safe) : '',
    options: inGroup.map(b => labelOf(b)).slice(0, 8),
  };
}
"""


async def _ensure_sanctions_safe(page, pk: str = "", url: str = "") -> str:  # noqa: ANN001
    """Guarantee any sanctions question is answered, and answered negatively.

    Vetoing a wrong answer is not enough: when the reader does not recognise the
    group (its markup varies by employer), nothing ever selects the safe option
    and a REQUIRED question is submitted blank. This sweeps the page for the
    group by its option text, unticks any country option, and ticks "None of the
    above". Runs regardless of what the model proposed. Returns a short note.
    """
    try:
        found = await page.evaluate(_SANCTIONS_SWEEP_JS)
    except Exception:  # noqa: BLE001
        return ""
    if not found:
        return ""
    try:
        for token in found.get("checkedRisky") or []:
            await page.locator(f'[data-ai-sanctions="{token}"]').first.click(timeout=2500)
            log.warning("sanctions: uncheck a country option that was ticked")
        note = ""
        if found.get("safe") == "ALREADY":
            note = f"already {found.get('safeLabel', '')[:40]}"
        elif found.get("safe"):
            await page.locator('[data-ai-sanctions="safe"]').first.click(timeout=2500)
            note = f"ticked {found.get('safeLabel', '')[:40]}"
        else:
            note = f"NO safe option found among {found.get('options')}"
            log.warning("sanctions group present but no 'None of the above': %s",
                        found.get("options"))
        log.info("sanctions sweep: %s", note)
        if pk:
            _emit(pk, "response", agent="browser", url=url,
                  detail=f"🛡 sanctions question — {note}")
        return note
    finally:
        try:
            await page.evaluate(
                "() => document.querySelectorAll('[data-ai-sanctions]')"
                ".forEach(e => e.removeAttribute('data-ai-sanctions'))")
        except Exception:  # noqa: BLE001
            pass


# Ashby renders a Yes/No question as two visible <button>s plus a HIDDEN
# <input type=checkbox> that carries the state. Every choice path here looked for
# the input, found the hidden one, and clicking it did nothing — so answerable
# questions ("do you require sponsorship?") were reported as unsettable and the
# application stopped. The visible control is what a person clicks, so click that.
_LOCATE_OPTION_CONTROL_JS = r"""
({q, v}) => {
  const norm = s => (s || '').toLowerCase().replace(/\s+/g, ' ').trim();
  const nq = norm(q), nv = norm(v);
  if (!nq || !nv) return 'NOQUESTION';

  // The field wrapper whose OWN text carries the question.
  const heads = [...document.querySelectorAll('label,legend,p,div,span')]
    .filter(e => e.children.length < 8 && norm(e.textContent).includes(nq.slice(0, 48)))
    .sort((a, b) => a.textContent.length - b.textContent.length);
  if (!heads.length) return 'NOQUESTION';

  for (const head of heads.slice(0, 3)) {
    let w = head;
    for (let i = 0; i < 5 && w.parentElement; i++) {
      w = w.parentElement;
      const opts = [...w.querySelectorAll('button, [role=radio], [role=option], label')]
        .filter(e => e.offsetParent !== null)          // visible only
        .filter(e => !/submit application/i.test(norm(e.textContent)));
      if (!opts.length) continue;
      const hit = opts.find(e => norm(e.textContent) === nv)
               || opts.find(e => { const t = norm(e.textContent);
                                   return t && t.length < 42
                                          && (t.includes(nv) || nv.includes(t)); });
      if (hit) {
        // Already chosen? Ashby marks the picked option on the button itself or
        // on the hidden input it controls.
        const box = hit.querySelector('input') ||
                    (hit.closest('div') || {})
                      .querySelector?.('input[type=checkbox],input[type=radio]');
        const on = (box && box.checked) ||
                   /selected|checked|_active|aria-checked="true"/i
                     .test(hit.className + ' ' + hit.outerHTML.slice(0, 200));
        if (on) return 'ALREADY';
        return hit;
      }
      if (opts.length >= 2) break;   // right group, wrong value — do not wander up
    }
  }
  return 'NOMATCH';
}
"""


async def _set_choices(page, choices: dict, pk: str = "", url: str = "") -> dict:  # noqa: ANN001
    """Select radio/checkbox options with REAL Playwright clicks — the only thing
    React groups (Ashby) accept. `choices` maps question -> option (str OR list for
    'select all that apply'). Verifies each click by re-locating (ALREADY = success),
    never clicks a wrong option, and never re-toggles an already-checked box. Returns
    {question: 'set' | failure-detail}."""
    out = {}
    for q, vals in choices.items():
        vals = vals if isinstance(vals, list) else [vals]
        vals = _safe_sanctions_answer(q, vals)
        if any(_is_self_id_affirmation(q, v) or _is_self_id_affirmation(str(v)) for v in vals):
            log.warning("refusing to self-declare: %s", str(q)[:70])
            out[q] = "refused: self-identification is the owner's to answer"
            continue
        statuses = []
        for v in vals:
            try:
                # Visible option control first (Ashby buttons); the classic
                # radio/checkbox locator is the fallback for ordinary forms.
                h = await page.evaluate_handle(_LOCATE_OPTION_CONTROL_JS, {"q": q, "v": str(v)})
                el = h.as_element()
                if el is None:
                    h = await page.evaluate_handle(_LOCATE_CHOICE_JS, {"q": q, "v": str(v)})
                    el = h.as_element()
                if el is None:
                    statuses.append(str(await h.json_value() or "NOQUESTION"))
                    continue
                await _bring_into_view(el)
                await el.click(timeout=3000)
                await page.wait_for_timeout(250)
                h2 = await page.evaluate_handle(_LOCATE_OPTION_CONTROL_JS, {"q": q, "v": str(v)})
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
    // Field wrapper's OWN unbound <label> (Ashby: Location, etc.) — a direct
    // child of the field container — before questionOf climbs to the section.
    for (let fw = el.parentElement, i = 0; i < 4 && fw; i++, fw = fw.parentElement) {
      const own = [...fw.children].find(c => c.tagName === 'LABEL');
      if (own) { const t = ntrim(own.textContent); if (t && t.length < 60) return t; }
    }
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
  let el = ctrls.find(e => {
    const lab = norm(labelOf(e));
    return lab && (lab.includes(k) || k.includes(lab));
  });
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


async def _set_resume_on_page(page: object, resume_path: str) -> int:
    """Set the résumé on the résumé field — and NOT on the cover-letter field.

    Setting every document-accepting input used to look harmless, but a form with
    a separate "Cover Letter" upload then received the résumé as the cover letter,
    which is what the recruiter opens. Prefer inputs that identify themselves as
    the résumé, always skip ones that identify as a cover letter, and fall back to
    the first document input only when nothing identifies itself at all.
    Returns how many inputs were set."""
    if page is None:
        return 0
    inputs = await page.query_selector_all('input[type="file"]')  # type: ignore[attr-defined]

    async def _describe(inp) -> str:  # noqa: ANN001
        """Everything the page says about this input, lowercased."""
        try:
            return (await inp.evaluate(
                """el => {
                     const wrap = el.closest('div,fieldset,section,label') || el;
                     return [el.name, el.id, el.getAttribute('aria-label'),
                             (el.labels && el.labels[0] && el.labels[0].textContent) || '',
                             (wrap.textContent || '').slice(0, 120)].join(' ');
                   }""") or "").lower()
        except Exception:  # noqa: BLE001
            return ""

    doc_inputs, resume_inputs = [], []
    for inp in inputs:
        try:
            accept = ((await inp.get_attribute("accept")) or "").lower()
        except Exception:
            accept = ""
        if accept and not any(k in accept for k in ("pdf", "application", "word", ".doc")):
            continue
        desc = await _describe(inp)
        if "cover letter" in desc or "coverletter" in desc:
            continue  # never the résumé's slot
        # An "Autofill from resume" box is a PARSER, not the résumé field. Feeding
        # it re-populates the form from whatever it reads and overwrites answers
        # already typed from the owner's approved facts — the apply task has always
        # told the agent to avoid it; the scripted path must too.
        if "autofill" in desc or "auto-fill" in desc or "parse" in desc:
            continue
        (resume_inputs if ("resume" in desc or "résumé" in desc or "cv" in desc)
         else doc_inputs).append(inp)

    n = 0
    for inp in (resume_inputs or doc_inputs or inputs):
        try:
            await inp.set_input_files(resume_path)
            n += 1
        except Exception:  # noqa: BLE001
            log.debug("could not set résumé on a file input", exc_info=True)
    # Say so either way: a silent 0 here is indistinguishable from "no résumé
    # needed", and that ambiguity hid a broken upload for days.
    (log.info if n else log.warning)(
        "résumé upload: set on %d/%d file input(s) from %s",
        n, len(inputs), resume_path or "(no résumé!)")
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


def _emit(pk: str, kind: str, **fields: object) -> None:
    if not pk:
        return
    from core.events import emit
    emit(kind, pk=pk, **fields)
