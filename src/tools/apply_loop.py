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
    _fill_human,
    _finalize_submit,
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

MAX_STEPS = 12  # a form is a handful of decisions; more than this is thrashing


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

Use the labels exactly as read_form gives them. Fill everything you can in as few
calls as possible, verify with read_form, then call ready_to_submit."""


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


async def _read(frame) -> list:  # noqa: ANN001
    try:
        return await frame.evaluate(_READ_FORM_JS)
    except Exception:  # noqa: BLE001
        return []


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

            fields = await _read(frame)
            if len(fields) < 2:
                return {"status": "unknown",
                        "detail": "No application form found on the page."}
            _emit(pk, "response", agent="browser", url=url,
                  detail=f"🔁 loop engine: {len(fields)} field(s)")

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
                            "fields": _ui_fields(await _read(frame))}

                for call in calls:
                    name = call.function.name
                    try:
                        args = json.loads(call.function.arguments or "{}")
                    except Exception:  # noqa: BLE001
                        args = {}
                    result: object = ""

                    if name == "read_form":
                        result = await _read(frame)

                    elif name == "fill_fields":
                        vals, refused = _guard(args.get("values") or {}, "text")
                        if vals:
                            await _fill_human(page, vals)
                            _emit(pk, "response", agent="browser", url=url,
                                  detail=f"⌨ filled {len(vals)}: {list(vals)[:4]}")
                        result = {"filled": list(vals), "refused": refused}

                    elif name == "select_choices":
                        vals, refused = _guard(args.get("answers") or {}, "choice")
                        got = await _set_choices(page, vals, pk, url) if vals else {}
                        result = {"set": got, "refused": refused}

                    elif name == "upload_resume":
                        n = await _set_resume_on_page(page, resume_path) if resume_path else 0
                        result = {"attached_to_inputs": n}

                    elif name == "need_human":
                        shot = base64.b64encode(await page.screenshot()).decode()
                        return {"status": "gate", "reason": "unknown_field",
                                "question": str(args.get("question") or
                                                "This form needs an answer only you can give."),
                                "screenshot_b64": shot, "drafted": drafted or None,
                                "fields": _ui_fields(await _read(frame))}

                    elif name == "ready_to_submit":
                        # Unconditional, exactly as the scripted engine does it: a
                        # sanctions question the model failed to group would
                        # otherwise go in blank.
                        await _ensure_sanctions_safe(page, pk, url)
                        await _click_fields_pass(page, pk, url)
                        return await _finalize_submit(page, facts, drafted, pk, url,
                                                      headed, model)

                    messages.append({"role": "tool", "tool_call_id": call.id,
                                     "name": name,
                                     "content": json.dumps(result, default=str)[:8000]})

            return {"status": "unknown",
                    "detail": f"The loop used all {MAX_STEPS} steps without submitting.",
                    "fields": _ui_fields(await _read(frame))}
        finally:
            await close()
