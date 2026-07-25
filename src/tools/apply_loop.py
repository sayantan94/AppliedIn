"""A third apply engine: a thin tool-calling loop over our own Playwright tools.

The scripted engine has no model in the click loop and handles a known ATS end to
end. browser-use sits at the other extreme: it brings its own DOM abstraction — an
indexed list of "interactive elements" — and re-derives on every step what this
codebase already knows. When that abstraction is wrong the agent cannot recover,
because the thing it is reasoning about is not the page. An Ashby Yes/No question
is two visible buttons plus a hidden checkbox; browser-use sees a checkbox, clicks
it, and reports success while nothing was set.

This engine keeps our DOM semantics and gives the model a much smaller job: look
at the fields we found and say which answer belongs in which one. Every mutation
still goes through the same deterministic helpers the scripted engine uses, and
the submit is `_finalize_submit` unchanged.

The reason that split matters is not tidiness. With browser-use, "never declare a
disability" and "never affirm a sanctions question" can only be *instructions* —
prose in a task prompt that a model may or may not honour. Here they are checks
inside the tools, so a model that tries anyway is refused rather than obeyed. The
guardrails stop being advice and become the interface.

Enable with APPLIEDIN_APPLY_ENGINE=loop.
"""

from __future__ import annotations

import base64
import json

from core.logging import get_logger

from .browser_apply import (
    _READ_FORM_JS,
    _click_fields_pass,
    _emit,
    _ensure_sanctions_safe,
    _fact_for,
    _finalize_submit,
    _form_frame,
    _is_error_page,
    _is_placeholder,
    _is_self_id_affirmation,
    _launch,
    _safe_sanctions_answer,
    _set_resume_on_page,
    _ui_fields,
    set_field,
)

log = get_logger(__name__)

MAX_STEPS = 18  # a form is a handful of decisions; more than this is thrashing


# --------------------------------------------------------------------------- #
# The tools the model is allowed to call. Deliberately few: this engine exists
# to decide WHICH ANSWER GOES WHERE, not to reinvent clicking.
# --------------------------------------------------------------------------- #

TOOLS = [
    {"type": "function", "function": {
        "name": "set_fields",
        "description": "Put values into the form. Keys are field labels exactly as "
                       "observed; values are what to enter. Works for every kind of "
                       "control — text, dropdown, radio, checkbox, location — you do "
                       "not need to know which. Send them all in one call.",
        "parameters": {"type": "object", "properties": {
            "values": {"type": "object", "description": "{field label: value}",
                       "additionalProperties": {"type": "string"}}},
            "required": ["values"]},
    }},
    {"type": "function", "function": {
        "name": "draft_answer",
        "description": "Draft a free-text answer (motivation, 'tell us about a "
                       "time…'). Always use this rather than writing prose yourself: "
                       "it is grounded in the résumé and the job description. Returns "
                       "the text — then pass it to set_fields.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string",
                         "description": "The question exactly as the form asks it."}},
            "required": ["question"]},
    }},
    {"type": "function", "function": {
        "name": "upload_resume",
        "description": "Attach the tailored résumé. Never goes to a cover-letter or "
                       "autofill-parser input.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "submit",
        "description": "Submit the application. Only when nothing required is left.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "ask_owner",
        "description": "Stop and ask the owner. Only for a required field with no "
                       "answer in the facts, or a login or security check. Never "
                       "guess a personal fact.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "One sentence."}},
            "required": ["question"]},
    }},
]

SYSTEM = """You are filling one job application form on behalf of its owner.

You work in a loop: you are shown the form's current state, you act, and you are
shown the new state. Keep going until nothing required is empty, then submit.

Work only from the approved facts you are given. You may reformat a fact to suit a
field, but never invent one — an address, a date, a salary or an employment detail
that is not in the facts is a fabrication in a document signed by a real person.
A field may arrive with `owner_answer`: that is the owner's own approved answer,
already matched for you. Use it as given, including for acknowledgements — they
have answered it, so do not ask again.

Answer questions about the owner's protected characteristics — disability, veteran
status, race, gender — only from an explicit approved fact. Otherwise leave them
alone: they are voluntary, and blank is a valid answer.

Free-text answers are a pitch. Always get them from draft_answer, which is
grounded in the owner's real work; never write prose yourself.

Batch every value into ONE set_fields call rather than going field by field. If a
field comes back unset, try a different value for it rather than repeating the
same one. Only call ask_owner for a required field you genuinely cannot source."""


def _guard(values: dict, kind: str) -> tuple[dict, list[str]]:
    """Drop anything the tool layer must never write, whatever the model asked.

    The scripted engine applies these same checks; putting them here too is the
    point of this engine — a refusal the model cannot talk its way past.
    """
    ok, refused = {}, []
    for label, value in (values or {}).items():
        if _is_placeholder(value):
            refused.append(f"{label}: placeholder text")
            continue
        if _is_self_id_affirmation(label, value) or _is_self_id_affirmation(str(value)):
            refused.append(f"{label}: self-identification is the owner's to answer")
            continue
        if kind == "choice":
            safe = _safe_sanctions_answer(label, [str(value)])
            if safe and str(safe[0]) != str(value):
                ok[label] = safe[0]          # sanctions answer corrected, not dropped
                refused.append(f"{label}: forced to the safe option {safe[0]!r}")
                continue
        ok[label] = value
    return ok, refused


async def _read(frame, facts: dict | None = None) -> list:  # noqa: ANN001
    """The form as we see it, with the owner's own answer attached to each field.

    Making the model search a hundred approved facts for the one that matches
    "Do you acknowledge that you have opened, read, and understood the Arbitration
    Agreement…" is work it does badly and we do deterministically. It kept asking
    the owner a question they had already answered simply because the wording did
    not match what they typed. The bank's answer now arrives with the field.
    """
    try:
        fields = await frame.evaluate(_READ_FORM_JS)
    except Exception:  # noqa: BLE001
        return []
    if not facts:
        return fields
    for f in fields:
        hit = _fact_for(f.get("label", ""), facts)
        if hit:
            f["owner_answer"] = hit[1]
    return fields


async def apply_loop(url: str, company: str, facts: dict, model: str, *, pk: str = "",
                     jd_text: str = "", resume_tex: str = "", github: str = "",
                     resume_path: str = "") -> dict:
    """Fill and submit `url` with a tool loop. Same return shape as the others."""
    from litellm import acompletion
    from playwright.async_api import async_playwright

    from .browser_apply import _reach_form, _site_rules

    drafted: dict = {}
    async with async_playwright() as p:
        page, close, headed = await _launch(p)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)
            await _reach_form(page)          # a job page is not an application
            frame = await _form_frame(page)

            body = (await page.evaluate("() => document.body.innerText || ''"))[:4000]
            if _is_error_page(page.url, body):
                return {"status": "unknown",
                        "detail": f"The posting did not load ({page.url})."}

            fields = await _read(frame, facts)
            if len(fields) < 2:
                return {"status": "unknown",
                        "detail": "No application form found on the page."}
            _emit(pk, "response", agent="browser", url=url,
                  detail=f"🔁 loop engine: {len(fields)} field(s)")

            # Anything the owner has already answered is applied before the model
            # is consulted at all. Asking a model whether to tick an arbitration
            # acknowledgement is asking it to weigh a legal waiver, which it will
            # decline — correctly — every time, and the owner gets stopped on a
            # question they answered days ago. Their explicit answer is not a
            # judgement call, so it does not go to a judge.
            pre_text, pre_choice = {}, {}
            for f in fields:
                ans = f.get("owner_answer")
                lbl = f.get("label") or ""
                if not ans or f.get("value") or not f.get("required"):
                    continue
                if _is_self_id_affirmation(lbl, ans) or _is_placeholder(ans):
                    continue
                # A LONE consent checkbox is not a choice group: there is no "Yes"
                # option to click, just a box to tick, and _set_choices correctly
                # reports NOMATCH for it. _fill_human already ticks a single
                # affirmative checkbox, so send it there instead. This is what kept
                # the arbitration acknowledgement unanswered.
                if f.get("type") in ("radio", "choice-group"):
                    pre_choice[lbl] = ans
                elif f.get("type") == "checkbox":
                    pre_text[lbl] = ans
                elif f.get("combo"):
                    continue          # geocoders need the pick pass; leave to the model
                elif f.get("type") not in ("file",):
                    pre_text[lbl] = ans
            for lbl, ans in {**pre_text, **pre_choice}.items():
                await set_field(page, lbl, ans, pk, url)
            if pre_text or pre_choice:
                _emit(pk, "response", agent="browser", url=url,
                      detail=f"✓ answered {len(pre_text) + len(pre_choice)} field(s) "
                             f"from your saved answers before asking the model")
                fields = await _read(frame, facts)

            messages = [
                {"role": "system", "content": SYSTEM + _site_rules(url, company)},
                {"role": "user", "content":
                    f"Company: {company}\nApproved facts:\n"
                    + json.dumps(facts, indent=2)[:6000]
                    + "\n\nFill this application."},
            ]

            def _observe(state: list) -> dict:
                """What the model needs to decide its next act, and nothing else."""
                todo = [f for f in state
                        if f.get("required") and not f.get("value")
                        and f.get("type") not in ("file",)]
                return {
                    "required_still_empty": [
                        {"label": f.get("label"), "type": f.get("type"),
                         "options": f.get("options") or None,
                         "owner_answer": f.get("owner_answer")}
                        for f in todo][:12],
                    "optional_still_empty": [
                        f.get("label") for f in state
                        if not f.get("required") and not f.get("value")
                        and f.get("type") not in ("file",)][:8],
                }

            messages.append({"role": "user",
                             "content": "Form state:\n"
                                        + json.dumps(_observe(fields), indent=1)[:4000]})

            for step in range(MAX_STEPS):
                resp = await acompletion(model=model, messages=messages, tools=TOOLS)
                msg = resp.choices[0].message
                calls = getattr(msg, "tool_calls", None) or []
                messages.append(msg.model_dump() if hasattr(msg, "model_dump") else msg)
                if not calls:
                    said = (getattr(msg, "content", "") or "").strip()
                    state = await _read(frame, facts)
                    if state and not [f for f in state if f.get("required")
                                      and not f.get("value")
                                      and f.get("type") not in ("file",)]:
                        break  # nothing left to do — fall through and submit
                    return {"status": "unknown",
                            "detail": f"Stopped at step {step + 1} without submitting."
                                      + (f" It said: {said[:160]}" if said else ""),
                            "fields": _ui_fields(state)}

                for call in calls:
                    name = call.function.name
                    try:
                        args = json.loads(call.function.arguments or "{}")
                    except Exception:  # noqa: BLE001
                        args = {}
                    result: object = ""

                    if name == "set_fields":
                        vals, refused = _guard(args.get("values") or {}, "choice")
                        outcome = {}
                        for lbl, v in vals.items():
                            outcome[lbl] = await set_field(page, lbl, v, pk, url)
                        done = [k for k, r in outcome.items() if r in ("set", "already")]
                        if done:
                            _emit(pk, "response", agent="browser", url=url,
                                  detail=f"✎ set {len(done)}: {done[:4]}")
                        result = {"per_field": outcome, "refused": refused}

                    elif name == "draft_answer":
                        from .narrative import draft_answer as _draft
                        q = str(args.get("question") or "")
                        text = _draft(q, company, jd_text, resume=resume_tex,
                                      model=model) if q else ""
                        if text and not _is_placeholder(text):
                            drafted[q] = text
                            _emit(pk, "response", agent="writer", url=url,
                                  detail=f"drafted answer → {q[:70]}")
                        result = {"answer": text or "no grounded answer — ask the owner"}

                    elif name == "upload_resume":
                        n = await _set_resume_on_page(page, resume_path) if resume_path else 0
                        result = {"attached_to_inputs": n}

                    elif name == "ask_owner":
                        shot = base64.b64encode(await page.screenshot()).decode()
                        return {"status": "gate", "reason": "unknown_field",
                                "question": str(args.get("question") or
                                                "This form needs an answer only you can give."),
                                "screenshot_b64": shot, "drafted": drafted or None,
                                "fields": _ui_fields(await _read(frame, facts))}

                    elif name == "submit":
                        await _ensure_sanctions_safe(page, pk, url)
                        await _click_fields_pass(page, pk, url)
                        return await _finalize_submit(page, facts, drafted, pk, url,
                                                      headed, model)

                    messages.append({"role": "tool", "tool_call_id": call.id,
                                     "name": name,
                                     "content": json.dumps(result, default=str)[:6000]})

                # OBSERVE after acting — the loop, not the model, keeps state fresh.
                fields = await _read(frame, facts)
                messages.append({"role": "user",
                                 "content": f"Form state (step {step + 1} of {MAX_STEPS}):\n"
                                            + json.dumps(_observe(fields), indent=1)[:4000]})

            # Out of steps. A complete form should be sent, not discarded.
            state = await _read(frame, facts)
            empty = [f for f in state if f.get("required") and not f.get("value")
                     and f.get("type") not in ("file",)]
            if state and not empty:
                _emit(pk, "response", agent="browser", url=url,
                      detail="⏱ out of steps but nothing required is empty — submitting")
            if state and not empty:
                await _ensure_sanctions_safe(page, pk, url)
                await _click_fields_pass(page, pk, url)
                return await _finalize_submit(page, facts, drafted, pk, url, headed, model)
            return {"status": "unknown",
                    "detail": f"Used all {MAX_STEPS} steps; {len(empty)} required "
                              f"field(s) still empty: "
                              + "; ".join(str(f.get("label"))[:40] for f in empty[:3]),
                    "fields": _ui_fields(state)}
        finally:
            await close()
