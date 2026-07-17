"""Confidence gate: high-confidence only via human-approved answers, never vibes."""

from __future__ import annotations

import pytest
from core.models import AnswerScope
from core.storage.answer_bank import AnswerBank
from worker.confidence import FieldResolution, all_high_confidence, resolve_field


@pytest.fixture
def bank(answer_bank_table):
    bank = AnswerBank(answer_bank_table)
    bank.seed_global(
        {
            "gender": "Male",
            "veteran status": "I am not a protected veteran",
            "work authorization": "Yes",
            "visa sponsorship": "Yes, H-1B",
            "notice period": "2 weeks",
            "first name": "Sayantan",
        }
    )
    return bank


def test_eeo_label_resolves_high_confidence_via_synonyms(bank):
    res = resolve_field("What is your gender?", "greenhouse", bank, "acme")
    assert res == FieldResolution(value="Male", high_confidence=True)

    res = resolve_field("Are you a veteran?", "greenhouse", bank, "acme")
    assert res.high_confidence and res.value == "I am not a protected veteran"


def test_ats_specific_synonym_layer(bank):
    res = resolve_field(
        "Are you legally authorized to work in the country where this job is located?",
        "greenhouse",
        bank,
        "acme",
    )
    assert res == FieldResolution(value="Yes", high_confidence=True)


def test_synonym_maps_variant_phrasing_to_canonical_fact(bank):
    res = resolve_field("When can you start?", "lever", bank, "acme")
    assert res == FieldResolution(value="2 weeks", high_confidence=True)


def test_unknown_essay_label_is_low_confidence(bank):
    res = resolve_field(
        "Describe a time when you disagreed with a teammate", "greenhouse", bank, "acme"
    )
    assert res == FieldResolution(value=None, high_confidence=False)


def test_recognized_label_without_banked_answer_is_low_confidence(bank):
    # "salary expectation" is in the synonym table but was never seeded/approved.
    res = resolve_field("Desired salary", "greenhouse", bank, "acme")
    assert res == FieldResolution(value=None, high_confidence=False)


def test_company_scoped_answer_is_high_confidence_for_that_company_only(bank):
    bank.put(
        "Why do you want to work here?",
        "Because of the infra team.",
        AnswerScope.COMPANY,
        company="acme",
        source="gate_approval",
    )
    hit = resolve_field("Why do you want to work here?", "greenhouse", bank, "acme")
    assert hit.high_confidence and hit.value == "Because of the infra team."

    miss = resolve_field("Why do you want to work here?", "greenhouse", bank, "other")
    assert miss == FieldResolution(value=None, high_confidence=False)


def test_all_high_confidence():
    high = FieldResolution(value="x", high_confidence=True)
    low = FieldResolution(value=None, high_confidence=False)
    assert all_high_confidence([high, high]) is True
    assert all_high_confidence([high, low]) is False
    assert all_high_confidence([]) is True
