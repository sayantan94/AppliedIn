"""ID construction and text-normalization helpers.

Centralized so the same normalization rules apply everywhere: dedup keys,
jd_hash, and answer-bank question labels must all normalize identically or
the deterministic guarantees in the HLD break.
"""

from __future__ import annotations

import hashlib
import re

_WHITESPACE = re.compile(r"\s+")
_PUNCT_EDGE = re.compile(r"^[\W_]+|[\W_]+$")
_PUNCT_ALL = re.compile(r"[^\w\s]")


def normalize_text(s: str) -> str:
    """Lowercase, collapse internal whitespace, strip leading/trailing punctuation.

    Used for jd_hash so cosmetic edits (extra spaces, trailing period, casing)
    do not defeat identical-repost detection.
    """
    s = s.lower()
    s = _WHITESPACE.sub(" ", s).strip()
    s = _PUNCT_EDGE.sub("", s)
    return s


def normalize_label(question: str) -> str:
    """Normalize a form question into a stable answer-bank sort key.

    Lowercase, remove all punctuation, collapse whitespace. So
    "Notice period?" and "notice  period" map to the same label.
    """
    s = question.lower()
    s = _PUNCT_ALL.sub(" ", s)
    s = _WHITESPACE.sub(" ", s).strip()
    return s


def jd_hash(jd_text: str) -> str:
    """SHA-256 of normalized JD text (hex). GSI key for identical-repost skip."""
    return hashlib.sha256(normalize_text(jd_text).encode("utf-8")).hexdigest()


def make_pk(company: str, job_id: str) -> str:
    """Dedup partition key: lowercased ``company#job_id``."""
    return f"{company.strip().lower()}#{job_id.strip()}"
