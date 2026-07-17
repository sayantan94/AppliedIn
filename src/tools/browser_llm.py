"""Pick the browser-use chat model from a model string, so browser-use can run
on Anthropic, OpenAI, or any OpenAI-compatible endpoint (OpenRouter → Kimi, …).

  claude-…                    -> ChatAnthropic          (ANTHROPIC_API_KEY)
  openrouter/<vendor>/<model> -> ChatOpenAI @ OpenRouter (OPENROUTER_API_KEY)
  openai/<model> | gpt-…      -> ChatOpenAI             (OPENAI_API_KEY)
"""

from __future__ import annotations

import os

from core.logging import get_logger

log = get_logger(__name__)


def _is_reasoning(model: str) -> bool:
    """Reasoning models (gpt-5*, o1/o3/o4) reject the sampling params browser-use
    sends by default (temperature≠1, frequency_penalty, top_p)."""
    m = model.lower()
    return "gpt-5" in m or m.startswith(("o1", "o3", "o4")) or "-o1" in m or "-o3" in m


def _openai_kwargs(slug: str) -> dict:
    """browser-use's ChatOpenAI defaults temperature=0.2 + frequency_penalty=0.05;
    reasoning models 400 on both, so null them (temperature must be exactly 1)."""
    if _is_reasoning(slug):
        return {"temperature": 1.0, "frequency_penalty": None, "top_p": None}
    return {}


def make_llm(model: str):
    """Return the browser-use chat object for `model` (routed by prefix)."""
    if model.startswith("openrouter/"):
        from browser_use import ChatOpenAI

        slug = model.split("/", 1)[1]  # e.g. "moonshotai/kimi-k2"
        log.info("browser-use on OpenRouter model: %s", slug)
        return ChatOpenAI(model=slug, base_url="https://openrouter.ai/api/v1",
                          api_key=os.environ.get("OPENROUTER_API_KEY"), **_openai_kwargs(slug))
    if model.startswith(("openai/", "gpt")):
        from browser_use import ChatOpenAI

        slug = model.split("/", 1)[-1]
        return ChatOpenAI(model=slug, api_key=os.environ.get("OPENAI_API_KEY"),
                          **_openai_kwargs(slug))
    from browser_use import ChatAnthropic

    return ChatAnthropic(model=model, api_key=os.environ.get("ANTHROPIC_API_KEY"))
