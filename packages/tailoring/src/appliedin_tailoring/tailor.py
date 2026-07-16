"""Tailoring agent — emphasis-only rewrite of the base resume against a JD.

The agent may reorder bullets, mirror JD vocabulary in bullet wording, and
adjust the summary/skills ordering. It must never invent or alter employers,
titles, dates, degrees, or certifications — and that promise is not trusted:
``truthfulness.validate`` mechanically checks the output before render.
"""

from __future__ import annotations

import json
from typing import Any

import yaml
from appliedin_core.logging import get_logger

from .scoring import _complete

log = get_logger(__name__)

_SYSTEM = (
    "You tailor resumes for a specific job description. Rewrite EMPHASIS ONLY: "
    "reorder bullets so the most relevant come first, mirror the job "
    "description's vocabulary in bullet wording, and adjust the summary and "
    "skills ordering. You must NEVER invent, alter, or omit employers, job "
    "titles, employment dates, degrees, institutions, or certifications — "
    "copy those fields verbatim from the input. Return ONLY a single JSON "
    "object with exactly the same schema and keys as the input resume. "
    "No markdown fences, no commentary."
)


def _parse_json(raw: str) -> dict:
    """Extract the JSON object from a model reply, tolerating fences/preamble."""
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"tailoring agent returned no JSON object: {raw!r}")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"tailoring agent returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("tailoring agent returned JSON that is not an object")
    return parsed


def tailor(base: dict, jd_text: str, model: Any = None) -> dict:
    """Return a tailored copy of ``base`` for ``jd_text`` (same schema).

    ``model`` follows the same injection contract as ``scoring.score_match``:
    None -> Strands Agent over ``get_model()``; a callable -> used directly
    (tests inject a stub and never touch Bedrock).
    """
    prompt = (
        "Base resume (YAML):\n"
        f"{yaml.safe_dump(base, sort_keys=False)}\n"
        "Job description:\n"
        f"{jd_text}\n\n"
        "Return the tailored resume as one JSON object with the same schema."
    )
    raw = _complete(prompt, _SYSTEM, model)
    return _parse_json(raw)
