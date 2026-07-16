"""Outbound message payloads — pure functions, no I/O.

Every gate DM carries reason-specific interactive buttons. WhatsApp caps
interactive messages at 3 reply buttons, so every set here MUST fit — the
unit tests enforce it. Button *ids* are deterministic slugs of the titles
(``button_id``), which is how the command router recognizes taps.
"""

from __future__ import annotations

import re

from appliedin_core.models import GateReason

MAX_BUTTONS = 3

_SLUG = re.compile(r"[^a-z0-9]+")


def button_id(title: str) -> str:
    """Deterministic slug used as the interactive button id.

    ``client.send_buttons`` stamps this on outbound buttons and
    ``commands.route`` matches taps against the same slug, so the two sides
    can never drift.
    """
    return _SLUG.sub("_", title.lower()).strip("_")


# Canonical button titles (single source of truth for slugs the router knows).
BTN_SKIP = "Skip"
BTN_MANUAL = "I'll do it manually"
BTN_RETRY = "Account created - retry"
BTN_APPROVE = "Approve"
BTN_APPROVE_SUBMIT = "Approve & submit"
BTN_COMPANY_ONLY = "Company only"
BTN_MARK_APPLIED = "Mark applied"

# Reason-specific button sets (HLD: WhatsApp caps at 3 buttons/message —
# every set below fits).
GATE_BUTTONS: dict[GateReason, list[str]] = {
    GateReason.CAPTCHA: [BTN_MANUAL, BTN_SKIP],
    GateReason.NO_ACCOUNT: [BTN_RETRY, BTN_MANUAL, BTN_SKIP],
    GateReason.UNKNOWN_FIELD: [BTN_APPROVE, BTN_COMPANY_ONLY, BTN_SKIP],
    GateReason.LOW_CONFIDENCE: [BTN_APPROVE, BTN_COMPANY_ONLY, BTN_SKIP],
    GateReason.GATED_MODE: [BTN_APPROVE_SUBMIT, BTN_SKIP],
    GateReason.FORM_DRIFT: [BTN_MANUAL, BTN_SKIP],
    GateReason.SUSPECTED_REPOST: [BTN_APPROVE_SUBMIT, BTN_SKIP],
    GateReason.SUBMIT_UNCERTAIN: [BTN_MARK_APPLIED, BTN_MANUAL, BTN_SKIP],
}


def receipt(company: str, title: str, pk: str, *, confirmation_url: str = "") -> dict:
    """Submission receipt payload (sent as a template outside the 24h window)."""
    text = f"Applied: {title} @ {company} ({pk})."
    if confirmation_url:
        text += f"\nConfirmation: {confirmation_url}"
    return {"text": text, "buttons": []}


def gate(
    reason: GateReason,
    *,
    pk: str,
    company: str = "",
    title: str = "",
    question: str = "",
    drafted_answer: str = "",
    proposed_scope: str = "global",
    screenshot_url: str = "",
) -> dict:
    """Gate DM payload: reason-specific text + <=3 reply buttons.

    For ``unknown_field`` / ``low_confidence`` the DM quotes the exact form
    question, the LLM's drafted answer, and the proposed save scope — reply
    "ok" (or tap Approve) to accept, or send free text to override (HLD).
    """
    header = f"Gate [{reason.value}] {title} @ {company} ({pk})".strip()
    lines = [header]

    if reason in (GateReason.UNKNOWN_FIELD, GateReason.LOW_CONFIDENCE):
        lines.append(f'The form asks: "{question}"')
        lines.append(f'Drafted answer: "{drafted_answer}"')
        scope_label = "a global fact" if proposed_scope == "global" else f"{company} only"
        lines.append(
            f"Save as {scope_label}? Reply 'ok' to approve, or send the answer to use instead."
        )
    elif reason is GateReason.CAPTCHA:
        lines.append("A CAPTCHA blocked the bot — it cannot be solved remotely.")
    elif reason is GateReason.NO_ACCOUNT:
        lines.append(
            "Auto-signup was blocked. Create the account, then tap "
            f"'{BTN_RETRY}' to re-run the application."
        )
    elif reason is GateReason.GATED_MODE:
        lines.append("Form filled and paused at submit — approve to send it.")
    elif reason is GateReason.FORM_DRIFT:
        lines.append("The form changed since it was reviewed; it needs fresh eyes.")
    elif reason is GateReason.SUSPECTED_REPOST:
        lines.append("This looks like a repost of a job we already applied to.")
    elif reason is GateReason.SUBMIT_UNCERTAIN:
        lines.append("Submit outcome is uncertain — please confirm what happened.")

    if screenshot_url:
        lines.append(f"Screenshot: {screenshot_url}")

    buttons = list(GATE_BUTTONS[reason])
    assert len(buttons) <= MAX_BUTTONS  # every set above fits the WhatsApp cap
    return {"text": "\n".join(lines), "buttons": buttons}
