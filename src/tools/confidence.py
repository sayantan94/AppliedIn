"""Deterministic confidence gate — defined, not vibes (HLD "Confidence" section).

A form field is high-confidence ONLY if it resolves to a human-approved answer:
either its normalized label hits the answer bank directly (company scope
shadows global), or a curated synonym table maps it to a canonical label the
bank holds. Anything else — essay questions, unrecognized dropdowns, any label
the tables and the bank have never seen — is low-confidence and gates the
whole application. LLM self-reported confidence is never used; the agentic
engine only proposes *labels*, and those labels come back through this module.

The synonym tables are curated by hand (edit + redeploy). A mapping that
proves right repeatedly gets promoted here manually — never automatically.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from core.ids import normalize_label
from core.storage.answer_bank import AnswerBank


@dataclass(frozen=True)
class FieldResolution:
    """Outcome of resolving one form field. ``value`` is None when nothing
    human-approved matches; ``high_confidence`` is the AUTO-path decision."""

    value: str | None
    high_confidence: bool


# Portal phrasing (normalized via normalize_label) -> canonical answer-bank label.
# Includes the EEO/self-identification block explicitly: these dropdowns appear
# on virtually every Greenhouse/Lever/Workday form and would otherwise gate
# everything (HLD).
COMMON_SYNONYMS: dict[str, str] = {
    # identity / contact
    "first name": "first name",
    "last name": "last name",
    "full name": "full name",
    "email": "email",
    "email address": "email",
    "phone": "phone",
    "phone number": "phone",
    "location": "location",
    "city": "location",
    "current location": "location",
    "linkedin profile": "linkedin",
    "linkedin url": "linkedin",
    "website": "website",
    "portfolio": "website",
    # work authorization / visa
    "are you legally authorized to work in the united states": "work authorization",
    "are you authorized to work in the united states": "work authorization",
    "do you have the legal right to work in the united states": "work authorization",
    "work authorization": "work authorization",
    "will you now or in the future require sponsorship": "visa sponsorship",
    "will you now or in the future require sponsorship for employment visa status": "visa sponsorship",  # noqa: E501
    "do you require sponsorship": "visa sponsorship",
    "do you now or will you in the future require visa sponsorship": "visa sponsorship",
    # logistics
    "notice period": "notice period",
    "what is your notice period": "notice period",
    "when can you start": "notice period",
    "earliest start date": "notice period",
    "are you willing to relocate": "relocation",
    "salary expectations": "salary expectation",
    "what are your salary expectations": "salary expectation",
    "desired salary": "salary expectation",
    "expected compensation": "salary expectation",
    # EEO / self-identification
    "gender": "gender",
    "what is your gender": "gender",
    "gender identity": "gender",
    "race": "ethnicity",
    "ethnicity": "ethnicity",
    "race ethnicity": "ethnicity",
    "are you hispanic or latino": "hispanic or latino",
    "veteran status": "veteran status",
    "are you a veteran": "veteran status",
    "protected veteran status": "veteran status",
    "disability status": "disability status",
    "do you have a disability": "disability status",
    "disability": "disability status",
}

# Per-ATS extras layered over COMMON_SYNONYMS (checked first). Grown manually
# during burn-in, one curated entry at a time.
ATS_SYNONYMS: dict[str, dict[str, str]] = {
    "greenhouse": {
        "are you legally authorized to work in the country where this job is located": (
            "work authorization"
        ),
        "please identify your gender": "gender",
        "i identify my gender as": "gender",
        "veteran status select one": "veteran status",
    },
    "lever": {
        "current company": "current company",
        "additional information": "additional information",
        "resume cv": "resume",
    },
}


def resolve_field(
    label: str, ats: str, answer_bank: AnswerBank, company: str
) -> FieldResolution:
    """Resolve one form label to an approved answer, deterministically.

    Lookup order: the label itself in the answer bank (company scope then
    global — the bank normalizes), then the synonym-mapped canonical label.
    Any hit is human-approved and therefore high-confidence; a miss means the
    field is free-form/unrecognized and must gate (high_confidence=False).
    """
    normalized = normalize_label(label)
    canonical = ATS_SYNONYMS.get(ats, {}).get(normalized) or COMMON_SYNONYMS.get(normalized)
    candidates = [normalized] if canonical in (None, normalized) else [normalized, canonical]
    for candidate in candidates:
        answer = answer_bank.lookup(candidate, company)
        if answer is not None:
            return FieldResolution(value=answer, high_confidence=True)
    return FieldResolution(value=None, high_confidence=False)


def all_high_confidence(resolutions: Iterable[FieldResolution]) -> bool:
    """True only when every resolved field may auto-submit. One low-confidence
    field gates the whole application (HLD guardrail 3)."""
    return all(r.high_confidence for r in resolutions)
