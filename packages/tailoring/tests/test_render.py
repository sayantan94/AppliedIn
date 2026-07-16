"""Typst render: to_typst is pure and testable; compile needs the binary."""

from __future__ import annotations

import shutil

import pytest
from appliedin_tailoring.render import render_pdf, to_typst

TAILORED = {
    "name": "Sayantan Bhowmik",
    "email": "sb@example.com",
    "summary": "Backend engineer focused on distributed systems.",
    "skills": ["Python", "AWS"],
    "experience": [
        {
            "employer": "Acme",
            "title": "Senior Engineer",
            "start": "2020-01",
            "end": "2023-06",
            "bullets": ["Built the billing service", "Led a team of 4"],
        }
    ],
    "education": [{"degree": "BSc Computer Science", "institution": "IIT Kharagpur"}],
    "certifications": ["AWS Solutions Architect Associate"],
}


def test_to_typst_produces_document_with_employer():
    doc = to_typst(TAILORED)
    assert doc.strip()
    assert "Acme" in doc
    assert "Senior Engineer" in doc
    assert "BSc Computer Science" in doc


def test_to_typst_escapes_markup_characters():
    doc = to_typst({"summary": "Cut costs 30% via #automation & C++"})
    assert "\\%" in doc
    assert "\\#" in doc
    assert "\\&" in doc


def test_render_pdf_without_binary_raises_clear_error(monkeypatch):
    monkeypatch.setattr("appliedin_tailoring.render.shutil.which", lambda name: None)
    with pytest.raises(RuntimeError, match="typst binary not found"):
        render_pdf(TAILORED)


@pytest.mark.skipif(shutil.which("typst") is None, reason="typst binary not installed")
def test_render_pdf_produces_pdf_bytes():
    pdf = render_pdf(TAILORED)
    assert pdf.startswith(b"%PDF")
