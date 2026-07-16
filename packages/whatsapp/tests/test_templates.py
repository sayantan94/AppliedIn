"""Gate templates: reason-specific button sets, all within WhatsApp's 3-button cap."""

from __future__ import annotations

from appliedin_core.models import GateReason
from appliedin_whatsapp import templates as t


def test_every_gate_reason_has_at_most_three_buttons():
    for reason in GateReason:
        payload = t.gate(reason, pk="acme#1", company="Acme", title="SWE")
        assert len(payload["buttons"]) <= t.MAX_BUTTONS, reason


def test_captcha_buttons():
    payload = t.gate(GateReason.CAPTCHA, pk="acme#1")
    assert payload["buttons"] == ["I'll do it manually", "Skip"]


def test_no_account_buttons():
    payload = t.gate(GateReason.NO_ACCOUNT, pk="acme#1")
    assert payload["buttons"] == ["Account created - retry", "I'll do it manually", "Skip"]


def test_unknown_field_quotes_question_draft_and_scope():
    payload = t.gate(
        GateReason.UNKNOWN_FIELD,
        pk="acme#1",
        company="Acme",
        title="SWE",
        question="What is your notice period?",
        drafted_answer="30 days",
        proposed_scope="global",
    )
    assert payload["buttons"] == ["Approve", "Company only", "Skip"]
    assert '"What is your notice period?"' in payload["text"]
    assert '"30 days"' in payload["text"]
    assert "global fact" in payload["text"]


def test_low_confidence_company_scope_prompt():
    payload = t.gate(
        GateReason.LOW_CONFIDENCE,
        pk="acme#1",
        company="Acme",
        question="Why Acme?",
        drafted_answer="Because...",
        proposed_scope="company",
    )
    assert payload["buttons"] == ["Approve", "Company only", "Skip"]
    assert "Acme only" in payload["text"]


def test_receipt_mentions_job():
    payload = t.receipt("Acme", "SWE", "acme#1", confirmation_url="https://x/y")
    assert "Acme" in payload["text"]
    assert "SWE" in payload["text"]
    assert "https://x/y" in payload["text"]
    assert payload["buttons"] == []


def test_button_id_is_stable_slug():
    assert t.button_id("Account created - retry") == "account_created_retry"
    assert t.button_id("I'll do it manually") == "i_ll_do_it_manually"
    assert t.button_id("Approve & submit") == "approve_submit"
