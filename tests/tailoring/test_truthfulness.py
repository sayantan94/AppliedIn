"""Truthfulness validator: bullets are free, structural facts are checksummed."""

from __future__ import annotations

from copy import deepcopy

from tailoring.truthfulness import validate

BASE = {
    "experience": [
        {
            "employer": "Acme",
            "title": "Senior Engineer",
            "start": "2020-01",
            "end": "2023-06",
            "bullets": ["Built the billing service", "Led a team of 4", "Cut costs 30%"],
        },
        {
            "employer": "Globex",
            "title": "Engineer",
            "start": "2017-03",
            "end": "2019-12",
            "bullets": ["Shipped the mobile API"],
        },
    ],
    "education": [{"degree": "BSc Computer Science", "institution": "IIT Kharagpur"}],
    "certifications": ["AWS Solutions Architect Associate"],
}


def test_reordered_and_reworded_bullets_pass():
    tailored = deepcopy(BASE)
    tailored["experience"][0]["bullets"] = [
        "Reduced infrastructure costs by 30%",  # reworded
        "Built the billing service",
        "Led a team of 4",  # reordered
    ]
    assert validate(BASE, tailored) == []


def test_invented_employer_fails():
    tailored = deepcopy(BASE)
    tailored["experience"][0]["employer"] = "Initech"
    violations = validate(BASE, tailored)
    assert violations
    assert any("Initech" in v and "employer" in v for v in violations)


def test_changed_date_range_fails():
    tailored = deepcopy(BASE)
    tailored["experience"][1]["end"] = "2024-01"  # stretched tenure
    violations = validate(BASE, tailored)
    assert violations
    assert any("date range" in v for v in violations)


def test_invented_certification_fails():
    tailored = deepcopy(BASE)
    tailored["certifications"] = ["AWS Solutions Architect Associate", "CKA"]
    assert any("CKA" in v for v in validate(BASE, tailored))


def test_dropping_an_entry_is_allowed():
    # Emphasis may omit an old role entirely; omission is not invention.
    tailored = deepcopy(BASE)
    tailored["experience"] = tailored["experience"][:1]
    assert validate(BASE, tailored) == []
