"""Deterministic truthfulness validator (HLD guardrail 4).

After tailoring, before rendering: every employer name, job title, date
range, degree, and certification in the tailored resume must exist verbatim
in the base resume. Bullets may be reworded or reordered — that is the whole
point of tailoring — but structural facts are checksummed here, mechanically,
on every application. Any mismatch routes the job to ``needs_human`` with the
violation list as the diff.
"""

from __future__ import annotations

from typing import Any


def _experience(resume: dict) -> list[dict]:
    return resume.get("experience") or []


def _education(resume: dict) -> list[dict]:
    return resume.get("education") or []


def _values(entries: list[dict], key: str) -> set[Any]:
    return {e.get(key) for e in entries} - {None}


def _date_ranges(entries: list[dict]) -> set[tuple[Any, Any]]:
    return {(e.get("start"), e.get("end")) for e in entries} - {(None, None)}


def validate(base: dict, tailored: dict) -> list[str]:
    """Return the list of structural-fact violations (empty list = pass)."""
    base_exp = _experience(base)
    base_edu = _education(base)
    employers = _values(base_exp, "employer")
    titles = _values(base_exp, "title")
    ranges = _date_ranges(base_exp)
    degrees = _values(base_edu, "degree")
    institutions = _values(base_edu, "institution")
    certifications = set(base.get("certifications") or [])

    violations: list[str] = []

    for exp in _experience(tailored):
        employer = exp.get("employer")
        if employer is not None and employer not in employers:
            violations.append(f"employer not in base resume: {employer!r}")
        title = exp.get("title")
        if title is not None and title not in titles:
            violations.append(f"job title not in base resume: {title!r}")
        rng = (exp.get("start"), exp.get("end"))
        if rng != (None, None) and rng not in ranges:
            violations.append(
                f"date range not in base resume: {rng[0]!r}-{rng[1]!r} (employer={employer!r})"
            )

    for edu in _education(tailored):
        degree = edu.get("degree")
        if degree is not None and degree not in degrees:
            violations.append(f"degree not in base resume: {degree!r}")
        institution = edu.get("institution")
        if institution is not None and institution not in institutions:
            violations.append(f"institution not in base resume: {institution!r}")

    for cert in tailored.get("certifications") or []:
        if cert not in certifications:
            violations.append(f"certification not in base resume: {cert!r}")

    return violations
