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


# Bookkeeping rows share the tracking table with jobs and are keyed
# ``meta#<kind>…``. A job at the company Meta is keyed ``meta#<job_id>`` by
# `make_pk`, so a prefix test cannot tell them apart — and for as long as it
# was used, every one of Meta's postings was treated as bookkeeping: hidden from
# the board, left out of the status index, never swept. The kinds are registered
# here and a test scans the source so a new one cannot be written unregistered.
INTERNAL_KINDS = ("watermark", "run", "profiles", "prefs", "dailycap", "undated", "age", "tracker")


def is_internal_pk(pk: str) -> bool:
    """Whether this tracking key is bookkeeping rather than a job."""
    pk = str(pk or "")
    if not pk.startswith("meta#"):
        return False
    kind = pk[5:].split("#", 1)[0]
    return kind in INTERNAL_KINDS
