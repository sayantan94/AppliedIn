"""Deterministic Lever application-form fill.

Lever postings render a single ``application-form`` with labeled inputs; the
shared label-driven strategy applies. Lever-specific label phrasings live in
``confidence.ATS_SYNONYMS["lever"]``, not here — engines never make
confidence decisions.
"""

from __future__ import annotations

from typing import Any

from core.models import JobRecord

from .. import FillResult, Resolver
from .base import fill_by_labels


class LeverFillEngine:
    ats = "lever"

    async def fill(self, page: Any, job: JobRecord, resolver: Resolver) -> FillResult:
        return await fill_by_labels(page, ats=self.ats, resolver=resolver)
