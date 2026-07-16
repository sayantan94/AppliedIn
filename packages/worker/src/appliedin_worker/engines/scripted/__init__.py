"""Per-ATS deterministic fill scripts — preferred where they exist because
they are testable and stable (HLD premise 3). Registry consumed by
``engines.pick_engine``."""

from __future__ import annotations

from .greenhouse import GreenhouseFillEngine
from .lever import LeverFillEngine

SCRIPTED_ENGINES: dict[str, type] = {
    GreenhouseFillEngine.ats: GreenhouseFillEngine,
    LeverFillEngine.ats: LeverFillEngine,
}

__all__ = ["SCRIPTED_ENGINES", "GreenhouseFillEngine", "LeverFillEngine"]
