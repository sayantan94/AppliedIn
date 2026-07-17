"""Shared label-driven fill strategy for scripted engines.

Minimal async Page surface used (keep this tiny so test fakes stay trivial):
    page.query_selector_all(selector) -> list[element]
    element.inner_text() -> str
    element.get_attribute(name) -> str | None
    page.fill(selector, value) -> None
"""

from __future__ import annotations

from typing import Any

from .. import FillResult, Resolver


async def fill_by_labels(
    page: Any,
    *,
    ats: str,
    resolver: Resolver,
    label_selector: str = "label",
) -> FillResult:
    """Walk the form's ``<label for=...>`` elements, resolving each through the
    deterministic confidence gate and filling only resolved values."""
    fields: dict = {}
    low_confidence: list[str] = []
    structure: list[dict] = []

    for element in await page.query_selector_all(label_selector):
        text = (await element.inner_text()).strip()
        if not text:
            continue
        target = await element.get_attribute("for")
        structure.append({"label": text, "for": target})
        resolution = resolver(text)
        if resolution.value is not None and target:
            await page.fill(f"#{target}", resolution.value)
            fields[text] = resolution.value
        if not resolution.high_confidence:
            low_confidence.append(text)

    snapshot = {"engine": "scripted", "ats": ats, "fields": structure}
    return FillResult(fields=fields, low_confidence_labels=low_confidence, form_snapshot=snapshot)
