"""Runtime pipeline controls the dashboard can flip while the daemon runs.

Env settings give the defaults; the UI overrides live (stored in Redis so the
worker, discovery thread, and web server all see the change instantly).

  apply_mode: "gated" — every apply waits for your approval (default)
              "auto"  — jobs scoring ≥ auto_min_score apply THEMSELVES, up to
                        the daily cap; only real blockers (CAPTCHA, missing
                        fact, login) wait for you. Find-and-apply while asleep.
  paused:     freezes the worker + discovery without stopping the daemon.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from .config import get_settings
from .logging import get_logger

log = get_logger(__name__)

_KEY = "appliedin:flags"


@lru_cache
def _redis() -> Any:
    import redis

    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


def get_flag(name: str, default: str = "") -> str:
    try:
        v = _redis().hget(_KEY, name)
        return v if v is not None else default
    except Exception:  # flags are best-effort — never break the pipeline
        return default


def set_flag(name: str, value: str) -> None:
    try:
        _redis().hset(_KEY, name, value)
    except Exception:
        log.debug("set_flag failed", exc_info=True)


def apply_mode() -> str:
    """'gated' (approve each apply) or 'auto' (apply overnight up to the cap)."""
    mode = get_flag("apply_mode", get_settings().apply_mode).lower()
    return mode if mode in ("gated", "auto") else "gated"


def note_llm_error(where: str, msg: str) -> None:
    """Record an LLM-provider failure (quota, auth, outage) so the dashboard can
    show a TOP-LEVEL banner — these errors otherwise degrade stages silently
    (e.g. the relevance screen passing whole feeds through)."""
    import json
    from datetime import datetime, timezone

    set_flag("llm_error", json.dumps({
        "where": where, "msg": msg[:300],
        "at": datetime.now(timezone.utc).isoformat()}))


def llm_error() -> dict | None:
    import json

    raw = get_flag("llm_error", "")
    if not raw:
        return None
    try:
        return json.loads(raw) or None
    except Exception:
        return None


def skipped_companies() -> set:
    """Companies (lowercase names) the owner excluded via the picker's skip
    toggles. Skipped companies sit out discovery AND processing whenever the
    run is un-scoped; explicitly picking one in the UI overrides the skip."""
    import json

    try:
        return {str(x).strip().lower()
                for x in json.loads(get_flag("skip_companies", "[]") or "[]") if x}
    except Exception:
        return set()


def company_filters() -> dict:
    """Per-company title keyword filters: {company_lower: [kw, ...]}. A company
    with a filter only keeps postings whose title contains one of its keywords
    (case-insensitive), overriding the global title prefs for that company. Set
    via the dashboard; empty = use the global prefs."""
    import json

    try:
        raw = json.loads(get_flag("company_filters", "{}") or "{}")
        return {str(k).lower(): [str(x) for x in v if str(x).strip()]
                for k, v in raw.items() if v}
    except Exception:
        return {}


def company_filter(company: str) -> list:
    return company_filters().get((company or "").strip().lower(), [])


def set_company_filter(name: str, keywords: list) -> dict:
    """Set (or clear, with an empty list) a company's title filter."""
    import json

    cur = company_filters()
    key = (name or "").strip().lower()
    kws = [k.strip() for k in (keywords or []) if k and k.strip()]
    if kws:
        cur[key] = kws
    else:
        cur.pop(key, None)
    set_flag("company_filters", json.dumps(cur))
    return cur


def title_matches_filter(title: str, keywords: list) -> bool:
    """True if no filter, or the title contains one of the keywords."""
    if not keywords:
        return True
    t = (title or "").lower()
    return any(k.lower() in t for k in keywords)


def set_company_skip(name: str, skip: bool) -> set:
    """Flip one company's skip state; returns the updated skip set."""
    import json

    cur = skipped_companies()
    key = (name or "").strip().lower()
    if key:
        (cur.add if skip else cur.discard)(key)
    set_flag("skip_companies", json.dumps(sorted(cur)))
    return cur


def paused() -> bool:
    return get_flag("paused", "no") == "yes"
