"""Deterministic Greenhouse application-form fill.

Greenhouse boards render one flat form with ``<label for="...">`` pairs
(first_name/last_name/email/phone + custom questions + the EEO block), so the
shared label-driven strategy covers it.
"""

from __future__ import annotations

from typing import Any

from core.models import JobRecord

from .. import FillResult, Resolver
from .base import fill_by_labels


class GreenhouseFillEngine:
    ats = "greenhouse"

    async def fill(self, page: Any, job: JobRecord, resolver: Resolver) -> FillResult:
        return await fill_by_labels(page, ats=self.ats, resolver=resolver)
