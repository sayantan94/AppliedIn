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

# Only expressly scoped posting evidence, never résumé or inferred geography.
# This finite vocabulary deliberately avoids geocoding incidental place mentions.
_LOCATION_NAMES = {
    "vietnam": "vietnam", "viet nam": "vietnam",
    "apac": "apac", "asia pacific": "apac", "asia-pacific": "apac",
    "worldwide": "world", "global": "world", "globally": "world",
    "anywhere": "world", "all countries": "world",
    "us": "us", "usa": "us", "united states": "us",
    "united states of america": "us", "canada": "canada",
    "japan": "japan", "singapore": "singapore", "australia": "australia",
    "eu": "europe", "europe": "europe", "european union": "europe",
    "uk": "uk", "united kingdom": "uk", "great britain": "uk",
    "gulf": "gcc", "gcc": "gcc",
    "saudi arabia": "saudi arabia", "united arab emirates": "uae",
    "uae": "uae", "qatar": "qatar", "oman": "oman",
    "bahrain": "bahrain", "kuwait": "kuwait",
    **{country: country for country in (
        "france", "germany", "italy", "spain", "portugal", "netherlands",
        "belgium", "austria", "switzerland", "ireland", "norway",
        "sweden", "denmark", "finland", "poland", "czechia",
        "czech republic", "greece", "romania", "bulgaria", "hungary",
        "croatia", "slovenia", "slovakia", "estonia", "latvia",
        "lithuania", "luxembourg", "malta", "cyprus", "iceland",
        "serbia", "montenegro", "albania", "north macedonia",
        "bosnia and herzegovina",
    )},
}
_ONSITE_FIT = set(_LOCATION_NAMES.values()) - {
    "apac", "world", "japan", "singapore", "australia",
}
_REMOTE_FIT = {"vietnam", "apac", "world"}
_LOCATION_FIELDS = {"location", "secondarylocations", "workplacetype", "isremote"}


def _location_scope(value: str) -> set[str] | None:
    """A complete bounded list of named regions, not tokens found in prose."""
    import re

    s = value.strip().casefold().strip(" .;:")
    s = re.sub(r"^(?:eligible\s+|from\s+|in\s+|the\s+)", "", s)
    s = re.sub(r"(?:[- ]only| residents? only| applicants? only)$", "", s).strip()
    parts = re.split(r"\s+(?:or|and)\s+|[,/]", s)
    if not parts or len(parts) > 6:
        return None
    named = {_LOCATION_NAMES.get(p.strip()) for p in parts}
    return named if None not in named else None


def _remote_scope(value: str) -> str | None:
    import re

    raw = value.strip()
    excluded = re.fullmatch(
        r"(.+?)\s+(?:excluding|except)\s+(?:residents?\s+(?:of|in)\s+)?(.+)",
        raw, re.I,
    )
    if excluded:
        broad = _location_scope(excluded.group(1))
        denied = _location_scope(excluded.group(2))
        if broad == {"apac"} and denied == {"vietnam"}:
            return "out_of_class"
        return None
    scopes = _location_scope(raw)
    if scopes is None:
        return None
    if scopes <= _REMOTE_FIT:
        return "in_class"
    if not scopes.intersection(_REMOTE_FIT):
        return "out_of_class"
    return None


def classify_location_fit(jd_text: str) -> str:
    """Classify explicitly stated posting-side geographic access only.

    Offices and a raw Ashby location describe a place, not a remote hiring
    perimeter. Missing or conflicting scoped evidence remains unknown.
    """
    import json
    import re

    if not isinstance(jd_text, str) or not jd_text.strip():
        return "unknown"
    text = jd_text[:100000]
    segments = []
    for clause in re.split(r"[;\n]", text):
        clause = clause.strip()
        if not clause:
            continue
        # Split sentences only after an explicit arrangement prefix. Splitting
        # Ashby JSON metadata could turn an office label into hiring evidence.
        if re.match(r"^(?:remote|on[- ]?site|hybrid)(?:\s|[-—–:,]|$)", clause, re.I):
            segments.extend(p.strip() for p in re.split(r"\.\s+(?=[A-Za-z])", clause)
                            if p.strip())
        else:
            segments.append(clause)
    metadata = {}
    for part in segments:
        match = re.fullmatch(
            r"(?:Ashby\s+)?(location|secondaryLocations|workplaceType|isRemote):\s*(.+)",
            part, re.I,
        )
        if match and match.group(1).casefold() in _LOCATION_FIELDS:
            try:
                value = json.loads(match.group(2))
            except (TypeError, ValueError):
                value = match.group(2).strip()
            metadata[match.group(1).casefold()] = value

    arrangement = metadata.get("workplacetype")
    arrangement = arrangement.strip().casefold() if isinstance(arrangement, str) else ""
    arrangement = arrangement.replace("-", "").replace(" ", "")
    remote_mode = arrangement == "remote"
    site_mode = arrangement in {"onsite", "hybrid"}
    for part in segments:
        if re.match(r"^(?:remote)(?:\s|[-—–:,]|$)", part, re.I):
            remote_mode = True
        if re.match(r"^(?:on[- ]?site|hybrid)(?:\s|[-—–:,]|$)", part, re.I):
            site_mode = True

    flag = metadata.get("isremote")
    if (remote_mode and site_mode or
            flag is False and remote_mode or flag is True and site_mode):
        return "unknown"
    remote_mode = remote_mode or flag is True
    if not remote_mode and not site_mode:
        return "unknown"

    findings = []
    for part in segments:
        if remote_mode:
            # Arrangement-plus-scope is explicit; a separate Location/Office
            # label is never promoted to a remote residency restriction.
            access = re.fullmatch(
                r"remote\s*(?:[-—–:]\s*|,\s*|from\s+|\s+)(.+)",
                part, re.I,
            )
            if access:
                scope = access.group(1)
                if scope.casefold() not in {"", "only", "work", "role"}:
                    verdict = _remote_scope(scope)
                    if verdict:
                        findings.append(verdict)
            restriction = re.search(
                r"\b(?:remote\s+)?(?:applicants?|candidates?|workers?)\s+"
                r"(?:must|need to|are required to)\s+"
                r"(?:be\s+)?(?:based|reside|live)\s+in\s+(.+)$",
                part, re.I,
            )
            if restriction:
                verdict = _remote_scope(restriction.group(1))
                if verdict:
                    findings.append(verdict)
            eligible = re.search(
                r"\b(?:eligible|open to|hiring)\s+(?:remote\s+)?"
                r"(?:candidates?\s+)?(?:from|in|across)\s+(.+)$",
                part, re.I,
            )
            if eligible:
                verdict = _remote_scope(eligible.group(1))
                if verdict:
                    findings.append(verdict)
        elif site_mode:
            worksite = re.fullmatch(
                r"(?:on[- ]?site|hybrid)\s*(?:[-—–:,]\s*|in\s+|at\s+)(.+)",
                part, re.I,
            )
            if not worksite:
                worksite = re.fullmatch(
                    r"(?:worksite|on[- ]?site work location|hybrid work location)"
                    r":\s*(.+)", part, re.I,
                )
            if worksite:
                places = _location_scope(worksite.group(1))
                if places:
                    choices = {"in_class" if p in _ONSITE_FIT else "out_of_class"
                               for p in places}
                    findings.extend(choices)
    return findings[0] if findings and len(set(findings)) == 1 else "unknown"



from core.logging import get_logger
from core.steering import messages
from core.models import JobRecord
from tools.schema import RelevanceResult

from .watchlist import Preferences

log = get_logger(__name__)


def _intent(prefs: Preferences) -> str:
    """Turn preferences.yaml into a natural-language brief for the agent."""
    if prefs.titles:
        target = ", ".join(prefs.titles)
        target_guidance = (
            "Match the job functions evident in posting titles to these configured "
            "roles, including reasonable functional title variants."
        )
    else:
        target = "unspecified (no target titles configured)"
        target_guidance = (
            "Do not infer a target job function or require a title/function match. "
            "Screen using configured hard constraints only; PREFERRED cues, if "
            "provided, are optional and never mandatory role filters."
        )
    lines = [
        f"TARGET ROLE: {target}. {target_guidance}",
        "ROLE FUNCTION VS RANK: Judge job function using functional evidence "
        "in each posting title, not corporate-rank tokens. Vice President and AVP "
        "alone establish neither a job function nor inferred seniority. Rank "
        "alone is neither a positive function match nor a categorical reason "
        "to reject. With configured target roles, omit rank-only titles from "
        "fit_indices when a match to the target function is unestablished due "
        "to missing functional evidence, not because the rank is disallowed. "
        "For rank-plus-functional-descriptor titles, assess the descriptor "
        "against configured target roles, not the corporate rank. An explicitly "
        "configured rank in PREFERRED remains a soft cue, not proof of function.",
    ]
    preferred = [*prefs.include_keywords, *prefs.seniority]
    if preferred:
        lines.append(f"PREFERRED (raises fit, not required): {', '.join(preferred)}.")
    if prefs.exclude_keywords:
        lines.append(f"AVOID (never a fit): {', '.join(prefs.exclude_keywords)}.")
    if prefs.locations:
        loc = ", ".join(prefs.locations)
        lines.append(
            f"LOCATION — REQUIRED: {loc}. Reject any posting whose on-site location "
            f"is clearly OUTSIDE of: {loc}. Remote within those counts. A blank or "
            "unknown location is allowed (don't reject on a missing location alone)."
        )
    if prefs.remote_only:
        lines.append("REMOTE ONLY: reject any posting that is clearly on-site or hybrid.")
    if prefs.notes.strip():
        lines.append("HARD CONSTRAINTS (reject any posting that violates these):\n"
                     + prefs.notes.strip())
    return "\n".join(lines)


def relevant(
    jobs: list[JobRecord], prefs: Preferences, *, model: str | None = None
) -> list[JobRecord]:
    """Return the subset of `jobs` the agent judges a plausible fit for the role."""
    if not jobs:
        return []
    from litellm import completion

    from core.config import get_settings

    model = model or get_settings().agent_model("relevance")
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
        resp = completion(model=model, messages=messages(prompt),
                          response_format=RelevanceResult)
        text = resp["choices"][0]["message"]["content"]
    except Exception as exc:
        # NEVER fail open: passing whole feeds through once flooded the board
        # with 600+ sales/legal postings during an OpenAI quota outage. Degrade
        # to the deterministic keyword/title gate and raise a TOP-LEVEL banner.
        log.error("relevance screen call failed (%s) — degrading to the keyword filter",
                  exc)
        from core import flags as _flags

        from .filters import stage1_match
        _flags.note_llm_error("relevance screen", str(exc))
        return [j for j in jobs if stage1_match(j, prefs)]

    try:
        keep = set(_parse(text).fit_indices)
    except Exception as exc:  # malformed output — don't silently drop everything
        log.warning("relevance screen parse failed (%s) — degrading to the keyword filter",
                    exc)
        from .filters import stage1_match
        return [j for j in jobs if stage1_match(j, prefs)]
    from core import flags as _flags

    _flags.clear_llm_error("relevance screen")
    return [j for i, j in enumerate(jobs) if i in keep]


def _parse(text: str) -> RelevanceResult:
    """Validate the model's reply into a RelevanceResult (tolerates a JSON object
    wrapped in prose)."""
    start, end = text.find("{"), text.rfind("}")
    blob = text[start : end + 1] if start != -1 and end > start else text
    return RelevanceResult.model_validate(json.loads(blob))
