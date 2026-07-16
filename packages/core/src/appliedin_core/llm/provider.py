"""Single choke point for LLM access.

Every agent (tailoring, agentic fill, WhatsApp Q&A) calls ``get_model()``. The
model is a config value, so switching from the interim Bedrock Claude Sonnet to
Muse Spark is one env change, not an architecture change (HLD constraint + OQ1).

Strands is imported lazily so packages that never build an agent (discovery,
dispatcher) can depend on core without pulling the SDK.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings, get_settings


def get_model(settings: Settings | None = None) -> Any:
    """Return a Strands model object selected by ``settings.llm_provider``."""
    settings = settings or get_settings()

    if settings.llm_provider == "bedrock":
        from strands.models import BedrockModel

        return BedrockModel(model_id=settings.llm_model, region_name=settings.aws_region)

    if settings.llm_provider == "muse_spark":
        # Swap point for OQ1: wire the Muse Spark endpoint here once confirmed.
        raise NotImplementedError("Muse Spark endpoint pending OQ1")

    raise ValueError(f"unknown llm_provider {settings.llm_provider!r}")
