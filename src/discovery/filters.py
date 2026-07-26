"""Stage-1 deterministic filter.

Cheap keyword/title/location gate that runs inside discovery so slow LLM
relevance scoring never blocks the poller (HLD premise 2). The LLM stage-2
score runs later, in the tailoring worker.
"""

from __future__ import annotations

from core.models import JobRecord

from .watchlist import Preferences


def _contains_any(haystack: str, needles: list[str]) -> bool:
    low = haystack.lower()
    return any(n.lower() in low for n in needles)


def stage1_match(job: JobRecord, prefs: Preferences) -> bool:
    text = f"{job.title}\n{job.jd_text}"

    if prefs.exclude_keywords and _contains_any(text, prefs.exclude_keywords):
        return False

    # include_keywords RAISE fit; they are not a requirement. This filter only
    # runs when the relevance screen fails, and rejecting everything without an
    # AI/agentic keyword there would quietly discard the plain "Senior Software
    # Engineer" roles the owner also asked for — a degraded screen should be
    # blunter, not stricter.

    if prefs.titles and not _contains_any(job.title, prefs.titles):
        return False

    if prefs.locations:
        loc = job.location.lower()
        remote_ok = prefs.remote_only and "remote" in loc
        if not remote_ok and not any(p.lower() in loc for p in prefs.locations):
            return False
    elif prefs.remote_only and "remote" not in job.location.lower():
        return False

    return True
