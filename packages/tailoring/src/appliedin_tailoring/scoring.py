"""Stage-2 match scoring — LLM scores the JD against the profile (0-10).

Below ``min_match_score`` (preferences.yaml) the handler marks the job
``skipped`` and nothing downstream runs. The model must answer with a bare
integer; parsing is defensive and clamps to [0, 10] because an unparseable
answer must never crash the pipeline (it scores 0, i.e. skip — the
conservative outcome).
"""

from __future__ import annotations

import re
from typing import Any

import yaml
from appliedin_core.llm.provider import get_model
from appliedin_core.logging import get_logger

log = get_logger(__name__)

_SYSTEM = (
    "You score how well a candidate profile matches a job description. "
    "Respond with a single integer from 0 to 10 and absolutely nothing else. "
    "0 means no overlap at all; 10 means an exceptional match."
)


def _complete(prompt: str, system_prompt: str, model: Any = None) -> str:
    """Run one prompt through an agent and return the raw text reply.

    ``model`` may be:
    - ``None`` — build a Strands Agent over ``get_model()`` (production path);
    - a Strands model instance — wrapped in an Agent with ``system_prompt``;
    - any plain callable ``prompt -> str`` (an Agent, or a test stub) — called
      directly. Strands model objects are not callable, so this cleanly
      separates the two, and tests never import strands at all.

    Strands is imported lazily so this module imports without the SDK.
    """
    if model is not None and callable(model):
        return str(model(prompt))

    from strands import Agent

    agent = Agent(model=model or get_model(), system_prompt=system_prompt)
    return str(agent(prompt))


def score_match(jd_text: str, profile: dict, model: Any = None) -> int:
    """Score JD-vs-profile relevance on 0-10 (clamped, never raises on junk)."""
    prompt = (
        "Job description:\n"
        f"{jd_text}\n\n"
        "Candidate profile (YAML):\n"
        f"{yaml.safe_dump(profile, sort_keys=False)}\n"
        "Score (single integer 0-10):"
    )
    raw = _complete(prompt, _SYSTEM, model)
    match = re.search(r"\d+", raw)
    if match is None:
        log.warning("unparseable match score %r — defaulting to 0 (skip)", raw)
        return 0
    return max(0, min(10, int(match.group())))
