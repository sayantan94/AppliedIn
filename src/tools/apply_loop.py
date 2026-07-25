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
    _fill_human,
    _finalize_submit,
    _fix_fields,
    _form_frame,
    _is_error_page,
    _is_placeholder,
    _is_self_id_affirmation,
    _launch,
    _safe_sanctions_answer,
    _set_choices,
    _set_resume_on_page,
    _ui_fields,
)

log = get_logger(__name__)

MAX_STEPS = 18  # a form is a handful of decisions; more than this is thrashing


# --------------------------------------------------------------------------- #
# The tools the model is allowed to call. Deliberately few: this engine exists
# to decide WHICH ANSWER GOES WHERE, not to reinvent clicking.
# --------------------------------------------------------------------------- #

TOOLS = [
    {"type": "function", "function": {
        "name": "read_form",
        "description": "Read every field on the application form: label, type, "
                       "options, whether it is required, and its current value. "
                       "Call this first, and again after filling to verify.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "fill_fields",
        "description": "Type values into text/textarea/date fields, using real "
                       "keystrokes. Keys are the field labels exactly as read_form "
                       "returned them.",
        "parameters": {"type": "object", "properties": {
            "values": {"type": "object",
                       "description": "{field label: value to type}",
                       "additionalProperties": {"type": "string"}}},
            "required": ["values"]},
    }},
    {"type": "function", "function": {
        "name": "select_choices",
        "description": "Answer radio/checkbox/dropdown questions by clicking the "
                       "visible option whose text matches. Keys are question "
                       "labels, values are the option text to choose.",
        "parameters": {"type": "object", "properties": {
            "answers": {"type": "object",
                        "description": "{question label: option text}",
                        "additionalProperties": {"type": "string"}}},
            "required": ["answers"]},
    }},
    {"type": "function", "function": {
        "name": "draft_answer",
        "description": "Draft a free-text answer (motivation, 'tell us about a "
                       "time…', anything open-ended). Always use this rather than "
                       "writing prose yourself: it is grounded in the résumé and "
                       "the job description. Returns the text to fill in.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string",
                         "description": "The question exactly as the form asks it."}},
            "required": ["question"]},
    }},
    {"type": "function", "function": {
        "name": "upload_resume",
        "description": "Attach the tailored résumé to the form's résumé field. "
                       "Never attaches to a cover-letter or autofill-parser input.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "ready_to_submit",
        "description": "Call when every required field is filled. The submit, "
                       "error self-heal and confirmation are handled for you.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "need_human",
        "description": "Call when a required field has no answer you can source "
                       "from the approved facts, or the page demands a login or "
                       "security check. Never guess a personal fact.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string",
                         "description": "What to ask the owner, in one sentence."}},
            "required": ["question"]},
    }},
]

SYSTEM = """You are filling one job application form on behalf of its owner.

Work only from the approved facts you are given. You may reformat a fact to suit a
field, but you may never invent one — an address, a date, a salary or an employment
detail that is not in the facts is a fabrication in a document signed by a real
person. If a required field has no answer in the facts, call need_human.

Answer questions about the owner's own protected characteristics — disability,
veteran status, race, gender — only from an explicit approved fact. If there is
none, leave the question alone; these are voluntary and blank is a valid state.

A legal acknowledgement — an arbitration agreement, a waiver, a certification
that the information is true — is the owner's to give and must never be ticked on
a guess. But an approved fact IS that answer: when the facts already say yes to
one, use it and move on. Stopping to ask a question the owner has already
answered is how a pipeline becomes something they have to babysit.

Free-text answers are a pitch, not a form filling exercise. Ground every claim in
the owner's real work from the facts and résumé, name the systems and the scale,
and say what they actually did rather than describing the role in general terms.
Write plain sentences: no dashes as connectors, no em dashes, no bulleted
fragments. If you cannot ground an answer in the facts, do not write it.

Each field read_form returns may carry an `owner_answer`: the owner's own approved
answer for that field, already matched for you. Use it as given. It is an answer
they have explicitly provided, including for acknowledgements, so do not ask them
again for something already there.

Use the labels exactly as read_form gives them. Fill everything you can in as few
calls as possible — batch every value into one fill_fields and one select_choices
rather than going field by field — verify once with read_form, then call
ready_to_submit. You have a limited number of steps and each tool result tells you
how many remain; an application left unsubmitted helps nobody, so once every
required field has a value, submit."""


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
                if f.get("type") in ("checkbox", "radio", "choice-group"):
                    pre_choice[lbl] = ans
                elif f.get("combo"):
                    continue          # geocoders need the pick pass; leave to the model
                elif f.get("type") not in ("file",):
                    pre_text[lbl] = ans
            if pre_text:
                await _fill_human(page, pre_text)
            if pre_choice:
                await _set_choices(page, pre_choice, pk, url)
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

            for step in range(MAX_STEPS):
                resp = await acompletion(model=model, messages=messages, tools=TOOLS)
                msg = resp.choices[0].message
                calls = getattr(msg, "tool_calls", None) or []
                messages.append(msg.model_dump() if hasattr(msg, "model_dump") else msg)
                if not calls:
                    # Stopping without submitting is not success. Say which of the
                    # two happened — a model that gave up on step 3 and one that
                    # ran out of steps need different fixes.
                    said = (getattr(msg, "content", "") or "").strip()
                    return {"status": "unknown",
                            "detail": f"The loop stopped at step {step + 1} without "
                                      f"submitting." + (f" It said: {said[:160]}" if said else ""),
                            "fields": _ui_fields(await _read(frame, facts))}

                for call in calls:
                    name = call.function.name
                    try:
                        args = json.loads(call.function.arguments or "{}")
                    except Exception:  # noqa: BLE001
                        args = {}
                    result: object = ""

                    if name == "read_form":
                        result = await _read(frame, facts)

                    elif name == "fill_fields":
                        vals, refused = _guard(args.get("values") or {}, "text")
                        # A combobox is not a text box. Typing into a geocoder
                        # leaves the text visible but nothing committed, so the
                        # model reads it back as empty and tries again until the
                        # step budget is gone — which is exactly how a location
                        # field ate a whole run. Send those through the real
                        # click, type and pick pass instead.
                        snapshot = await _read(frame, facts)
                        combos = {f.get("label") for f in snapshot
                                  if f.get("combo") or "location" in
                                  str(f.get("label") or "").lower()}
                        typed = {k: v for k, v in vals.items() if k not in combos}
                        picked = {k: v for k, v in vals.items() if k in combos}
                        if typed:
                            await _fill_human(page, typed)
                            _emit(pk, "response", agent="browser", url=url,
                                  detail=f"⌨ filled {len(typed)}: {list(typed)[:4]}")
                        if picked:
                            # a geocoder matches on the city, not the full string
                            picked = {k: (v.split(",")[0].strip()
                                          if "location" in k.lower() else v)
                                      for k, v in picked.items()}
                            _emit(pk, "response", agent="browser", url=url,
                                  detail=f"🎯 combobox: {list(picked)}")
                            await _fix_fields(page, picked, pk, url)
                        result = {"filled": list(typed), "picked_from_dropdown": list(picked),
                                  "refused": refused}

                    elif name == "select_choices":
                        vals, refused = _guard(args.get("answers") or {}, "choice")
                        got = await _set_choices(page, vals, pk, url) if vals else {}
                        result = {"set": got, "refused": refused}

                    elif name == "draft_answer":
                        # The same writer the scripted engine uses: grounded in the
                        # owner's real work, and already tuned for how they want to
                        # be represented. A second prose voice in the pipeline would
                        # be one more thing to keep in sync.
                        from .narrative import draft_answer as _draft
                        q = str(args.get("question") or "")
                        text = _draft(q, company, jd_text, resume=resume_tex,
                                      model=model) if q else ""
                        if text and not _is_placeholder(text):
                            drafted[q] = text
                            _emit(pk, "response", agent="writer", url=url,
                                  detail=f"drafted answer → {q[:70]}")
                        result = {"answer": text or
                                  "no grounded answer available — ask the owner"}

                    elif name == "upload_resume":
                        n = await _set_resume_on_page(page, resume_path) if resume_path else 0
                        result = {"attached_to_inputs": n}

                    elif name == "need_human":
                        shot = base64.b64encode(await page.screenshot()).decode()
                        return {"status": "gate", "reason": "unknown_field",
                                "question": str(args.get("question") or
                                                "This form needs an answer only you can give."),
                                "screenshot_b64": shot, "drafted": drafted or None,
                                "fields": _ui_fields(await _read(frame, facts))}

                    elif name == "ready_to_submit":
                        # Unconditional, exactly as the scripted engine does it: a
                        # sanctions question the model failed to group would
                        # otherwise go in blank.
                        await _ensure_sanctions_safe(page, pk, url)
                        await _click_fields_pass(page, pk, url)
                        return await _finalize_submit(page, facts, drafted, pk, url,
                                                      headed, model)

                    messages.append({
                        "role": "tool", "tool_call_id": call.id, "name": name,
                        "content": json.dumps(
                            {"result": result,
                             "steps_remaining": MAX_STEPS - step - 1},
                            default=str)[:8000]})

            # Out of steps. If the form is actually complete, submitting it is
            # obviously right — discarding a finished application because the
            # model spent its budget verifying would waste the whole run and
            # leave the owner to redo it by hand.
            state = await _read(frame, facts)
            empty = [f for f in state if f.get("required") and not f.get("value")
                     and f.get("type") not in ("file",)]
            if state and not empty:
                _emit(pk, "response", agent="browser", url=url,
                      detail="⏱ out of steps but every required field is filled — submitting")
                await _ensure_sanctions_safe(page, pk, url)
                await _click_fields_pass(page, pk, url)
                return await _finalize_submit(page, facts, drafted, pk, url, headed, model)
            return {"status": "unknown",
                    "detail": f"The loop used all {MAX_STEPS} steps and "
                              f"{len(empty)} required field(s) are still empty: "
                              + "; ".join(str(f.get("label"))[:40] for f in empty[:3]),
                    "fields": _ui_fields(state)}
        finally:
            await close()
