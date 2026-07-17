"""Fill engines — scripted (per-ATS deterministic) and agentic (Strands).

Both engines share one contract: they locate form fields and route every
label through the injected resolver (:func:`worker.confidence.
resolve_field` bound to the job's ATS/company). The engine decides HOW to find
fields; it never decides confidence — that stays deterministic (HLD).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.models import JobRecord

from ..confidence import FieldResolution

# An engine receives this bound resolver; it maps a raw form label to a
# (value, high_confidence) decision deterministically.
Resolver = Callable[[str], FieldResolution]


@dataclass
class FillResult:
    """What a fill pass produced: values written, labels that must gate, and
    the form-structure snapshot persisted for the approval-resume diff."""

    fields: dict = field(default_factory=dict)
    low_confidence_labels: list[str] = field(default_factory=list)
    form_snapshot: dict = field(default_factory=dict)


class FillEngine(Protocol):
    async def fill(self, page: Any, job: JobRecord, resolver: Resolver) -> FillResult: ...


def pick_engine(ats: str) -> FillEngine:
    """Scripted engine where one exists for the ATS, else agentic (HLD premise 3).

    Imports are local to dodge the package-init cycle and keep the agentic
    module (with its lazy Strands import) unloaded unless needed.
    """
    from .agentic import AgenticFillEngine
    from .scripted import SCRIPTED_ENGINES

    engine_cls = SCRIPTED_ENGINES.get(ats)
    return engine_cls() if engine_cls is not None else AgenticFillEngine()
