"""Agentic fill engine — a Strands agent discovers the form; confidence stays
deterministic (HLD premise 3 + "Confidence" section).

The LLM's ONLY job is DOM discovery: given the page it proposes
``[{"label": ..., "selector": ...}, ...]``. Every proposed label is then routed
through the injected resolver exactly like a scripted engine's labels — the
model never supplies values for the AUTO path and never marks a field
high-confidence. A free-form field therefore gates no matter what the model
says about it.

Strands is imported lazily inside the default discoverer so this module (and
the whole package) imports without the SDK; tests inject a fake discoverer.

Minimal async Page surface used by the default discoverer:
    page.content() -> str        (fed to the model)
    page.fill(selector, value)   (shared with scripted engines)
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from core.logging import get_logger
from core.models import JobRecord

from . import FillResult, Resolver

log = get_logger(__name__)

# Injected form-discoverer: (page, job) -> [{"label": str, "selector": str}, ...]
Discoverer = Callable[[Any, JobRecord], Awaitable[list[dict]]]

_MAX_HTML_CHARS = 60_000

_SYSTEM_PROMPT = (
    "You map job-application form HTML to fields. Reply with ONLY a JSON array; "
    'each element is {"label": <visible question text>, "selector": <CSS selector '
    "of the input to fill>}. Include every fillable field. No prose."
)

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


class AgenticFillEngine:
    """FillEngine for portals with no scripted adapter (custom portals,
    one-off ATSes, Workday tenants). Starts gated and earns auto through
    burn-in like any other portal."""

    def __init__(self, discover: Discoverer | None = None) -> None:
        self._discover = discover or _strands_discover

    async def fill(self, page: Any, job: JobRecord, resolver: Resolver) -> FillResult:
        proposed = await self._discover(page, job)

        fields: dict = {}
        low_confidence: list[str] = []
        structure: list[dict] = []
        for item in proposed:
            label = str(item.get("label", "")).strip()
            selector = item.get("selector")
            if not label:
                continue
            structure.append({"label": label, "selector": selector})
            # Deterministic gate: the model proposed the label; it does NOT
            # get a vote on the value or the confidence.
            resolution = resolver(label)
            if resolution.value is not None and selector:
                await page.fill(selector, resolution.value)
                fields[label] = resolution.value
            if not resolution.high_confidence:
                low_confidence.append(label)

        snapshot = {"engine": "agentic", "ats": job.ats, "fields": structure}
        return FillResult(
            fields=fields, low_confidence_labels=low_confidence, form_snapshot=snapshot
        )


async def _strands_discover(page: Any, job: JobRecord) -> list[dict]:
    """Default discoverer: a Strands agent over the page HTML (lazy import)."""
    from core.llm.provider import get_model
    from strands import Agent

    html = (await page.content())[:_MAX_HTML_CHARS]
    agent = Agent(model=get_model(), system_prompt=_SYSTEM_PROMPT)
    reply = str(
        agent(f"Application form for {job.title!r} at {job.company!r}:\n\n{html}")
    )
    return parse_proposed_fields(reply)


def parse_proposed_fields(text: str) -> list[dict]:
    """Defensively parse the model's reply into label/selector dicts."""
    match = _JSON_ARRAY_RE.search(text)
    if match is None:
        log.warning("agentic discoverer returned no JSON array")
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        log.warning("agentic discoverer returned invalid JSON")
        return []
    return [item for item in data if isinstance(item, dict)]
