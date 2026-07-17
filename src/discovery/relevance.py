"""Agentic stage-1 relevance — an agent decides which postings fit, not brittle
substring rules.

One LLM call per company over the posting titles: given the candidate's target
role and soft preferences (preferences.yaml), it returns the postings worth
sending down the pipeline, understanding role VARIANTS (a "Software Engineer"
target also fits SDE, Backend/Platform/ML/AI Engineer, …). The deeper stage-2
score still runs per job later. Mode-aware model via LiteLLM (Anthropic local /
Bedrock cloud).

Fail-open: if the screen errors or returns garbage, every posting passes through
rather than silently dropping them all — stage-2 is the backstop.
"""

from __future__ import annotations

import json

from core.logging import get_logger
from core.models import JobRecord
from tools.schema import RelevanceResult

from .watchlist import Preferences

log = get_logger(__name__)


def _intent(prefs: Preferences) -> str:
    """Turn preferences.yaml into a natural-language brief for the agent."""
    target = ", ".join(prefs.titles) or "Software Engineer"
    lines = [
        f"TARGET ROLE: {target}. These are HANDS-ON engineering roles that write "
        "code. Count reasonable variants as a fit — Software Development Engineer "
        "(SDE/SWE), Backend / Frontend / Full-Stack / Platform / Infrastructure / "
        "Machine Learning / AI / Applied-Science (coding) Engineer at a similar "
        "level.",
        "NOT A FIT even though they sound technical: Program Manager (TPM), "
        "Project Manager, Product Manager, Solutions/Enterprise Architect, Support "
        "Engineer, Solutions/Sales/Field Engineer, Customer Engineer, Data Analyst, "
        "Designer, Recruiter, Android / iOS / mobile-app Engineer, and any "
        "people-management role (Manager/Director of …). Reject these.",
    ]
    preferred = [*prefs.include_keywords, *prefs.seniority]
    if preferred:
        lines.append(f"PREFERRED (raises fit, not required): {', '.join(preferred)}.")
    if prefs.exclude_keywords:
        lines.append(f"AVOID (never a fit): {', '.join(prefs.exclude_keywords)}.")
    if prefs.locations:
        loc = ", ".join(prefs.locations)
        lines.append(
            f"LOCATION — REQUIRED: {loc}. Reject any posting whose location is "
            "clearly OUTSIDE the United States. A blank/unknown location is "
            "allowed (don't reject on a missing location alone)."
            + (" Remote must be US-based." if not prefs.remote_only else "")
        )
    return "\n".join(lines)


def relevant(
    jobs: list[JobRecord], prefs: Preferences, *, model: str | None = None
) -> list[JobRecord]:
    """Return the subset of `jobs` the agent judges a plausible fit for the role."""
    if not jobs:
        return []
    from litellm import completion

    from core.config import get_settings

    model = model or get_settings().litellm_model
    listing = "\n".join(
        f"{i}. {j.title}  [location: {j.location or 'unknown'}]" for i, j in enumerate(jobs)
    )
    prompt = (
        "You screen job postings for a candidate. Decide which of the titles "
        "below are a plausible fit for the target role. Be inclusive about "
        "variants and adjacent titles; be strict about the AVOID list.\n\n"
        f"{_intent(prefs)}\n\n"
        f"POSTINGS (numbered):\n{listing}\n\n"
        'Return ONLY a JSON object of the form {"fit_indices": [0, 3, 4]} listing '
        "the 0-based numbers that fit. Use an empty list if none fit."
    )
    try:
        resp = completion(model=model, messages=[{"role": "user", "content": prompt}],
                          response_format=RelevanceResult)
        text = resp["choices"][0]["message"]["content"]
    except Exception as exc:
        log.error("relevance screen call failed (%s) — passing all %d postings through",
                  exc, len(jobs))
        return jobs

    try:
        keep = set(_parse(text).fit_indices)
    except Exception as exc:  # malformed output — don't silently drop everything
        log.warning("relevance screen parse failed (%s) — passing all through", exc)
        return jobs
    return [j for i, j in enumerate(jobs) if i in keep]


def _parse(text: str) -> RelevanceResult:
    """Validate the model's reply into a RelevanceResult (tolerates a JSON object
    wrapped in prose)."""
    start, end = text.find("{"), text.rfind("}")
    blob = text[start : end + 1] if start != -1 and end > start else text
    return RelevanceResult.model_validate(json.loads(blob))
