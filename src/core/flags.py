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

from datetime import UTC
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
    """How an approved job gets submitted.

      gated    — the pipeline drives a browser, you approve each apply (default)
      auto     — the pipeline applies by itself up to the daily cap
      assisted — the pipeline stops at TAILORED and you finish each application
                 in your OWN browser with the extension. Slower per job, but the
                 employer sees a real session instead of an automated one, which
                 is what avoids the bot challenges that block the other modes.
    """
    mode = get_flag("apply_mode", get_settings().apply_mode).lower()
    return mode if mode in ("gated", "auto", "assisted") else "gated"


def assisted() -> bool:
    """True when applications are finished by hand via the browser extension."""
    return apply_mode() == "assisted"


def apply_concurrency() -> int:
    """How many applications may run AT ONCE, always to different companies.

    A runtime flag rather than an env var, because the right number depends on
    what the machine is doing right now: three browser sessions is comfortable
    while you are away, and one is what you want when you are using the laptop.
    Clamped to 1..6 — the per company lease already prevents the collision that
    matters, and beyond a handful the sessions simply fight for the screen.
    """
    raw = get_flag("apply_concurrency", "")
    if not raw:
        import os

        raw = os.environ.get("APPLIEDIN_APPLY_CONCURRENCY", "3")
    try:
        return max(1, min(6, int(raw)))
    except (TypeError, ValueError):
        return 3


def browser_headless() -> bool:
    """Whether apply runs Chrome HEADLESS (no visible windows). Runtime flag
    overrides the env default. Note: a CAPTCHA / human handoff can't open a
    visible window when headless — it surfaces on the board with a screenshot."""
    v = get_flag("headless", "")
    if v in ("yes", "no"):
        return v == "yes"
    return bool(getattr(get_settings(), "browser_headless", False))


def note_llm_error(where: str, msg: str) -> None:
    """Record an LLM-provider failure (quota, auth, outage) so the dashboard can
    show a TOP-LEVEL banner — these errors otherwise degrade stages silently
    (e.g. the relevance screen passing whole feeds through)."""
    import json
    from datetime import datetime

    set_flag("llm_error", json.dumps({
        "where": where, "msg": msg[:300],
        "at": datetime.now(UTC).isoformat()}))


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


# --- per-company preference overrides -------------------------------------
# A company filter narrows titles on top of the global preferences. That is not
# enough on its own: "Google, but only Staff+, and drop the score bar to 6" needs
# the SAME fields as the global preferences, overridden for one company and
# inherited everywhere they are left unset. These are stored separately from the
# title filter so an existing filter keeps working untouched.
_PREF_KEY = "company_prefs"

# Every field the global preferences panel offers, except `github`, which is
# about the candidate rather than the company.
COMPANY_PREF_FIELDS = ("titles", "include_keywords", "exclude_keywords",
                       "seniority", "locations", "min_match_score",
                       "max_new_per_run", "remote_only", "notes",
                       # Which identity this company's applications go out under.
                       # Not part of the global Preferences object — it is a
                       # per-run choice there — but per company it is exactly a
                       # preference: "anything at Adobe uses my personal 2 address".
                       "profile_id",
                       # Only consider roles the employer published within this many
                       # hours. 0 or absent means no age limit, which is the old
                       # behaviour and stays the default: a company whose board
                       # publishes no dates would otherwise return nothing at all.
                       "max_age_hours")


def company_prefs() -> dict:
    """{company_lower: {field: value}} — only the fields that were overridden."""
    import json

    try:
        raw = json.loads(get_flag(_PREF_KEY, "{}") or "{}")
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for co, over in (raw or {}).items():
        if not isinstance(over, dict):
            continue
        clean = {k: v for k, v in over.items() if k in COMPANY_PREF_FIELDS}
        if clean:
            out[str(co).lower()] = clean
    return out


def company_pref(company: str) -> dict:
    return company_prefs().get((company or "").strip().lower(), {})


def set_company_pref(name: str, overrides: dict) -> dict:
    """Set a company's overrides. A field mapped to None/'' /[] is REMOVED, which
    is how you go back to inheriting the global value; passing {} clears them all.
    Removal has to be explicit because "" is a legitimate way to say "no titles"
    and would otherwise be indistinguishable from "inherit"."""
    import json  # noqa: F401 — used by the early return below too

    cur = company_prefs()
    key = (name or "").strip().lower()
    # An empty mapping means "this company has no overrides at all". Merging it
    # field-by-field would be a no-op and silently leave the old ones in place,
    # so reset-to-global has to be handled before the merge, not by it.
    if not overrides:
        cur.pop(key, None)
        set_flag(_PREF_KEY, json.dumps(cur))
        return cur
    over = dict(cur.get(key) or {})
    for field, value in overrides.items():
        if field not in COMPANY_PREF_FIELDS:
            continue
        if value is None or value == "" or value == []:
            over.pop(field, None)
        else:
            over[field] = value
    if over:
        cur[key] = over
    else:
        cur.pop(key, None)
    set_flag(_PREF_KEY, json.dumps(cur))
    return cur


def effective_prefs(company: str, base):
    """`base` with this company's overrides applied. Returns a COPY, so the
    caller's global preferences object is never mutated — one company's override
    leaking into the next company's run would be silent and hard to spot."""
    over = company_pref(company)
    if not over:
        return base
    try:
        merged = base.model_copy(update=over)      # pydantic v2
    except AttributeError:
        import copy

        merged = copy.deepcopy(base)
        for k, v in over.items():
            setattr(merged, k, v)
    return merged


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


# --- worker heartbeat ------------------------------------------------------
# The pipeline must never LOOK healthy while doing nothing. `python -m server`
# is uvicorn ONLY — the board renders and /stats returns 200, but no job is ever
# discovered, scored, tailored or applied. `python -m daemon` is the real entry
# point: it spawns the discovery/evaluate/apply loops and then serves the web app.
# Only the daemon beats, so a workerless process is detectable instead of silent.
#
# The beat comes from a dedicated thread that reports which loops are ALIVE
# (threading.enumerate), not which are idle — so a worker legitimately busy for
# ten minutes inside one browser apply still reads as healthy.

# Loops the pipeline cannot function without. Discovery is deliberately absent:
# it's opt-out via APPLIEDIN_DISCOVERY=off, so its absence is a choice, not a fault.
_REQUIRED_WORKERS = ("evaluate", "apply")
_HEARTBEAT_STALE_SECONDS = 90.0  # beat is every ~10s; 90s tolerates a slow host


def beat_workers(names: list[str], *, now: float | None = None) -> None:
    """Record that `names` worker loops are alive right now."""
    import json
    import time

    set_flag("heartbeat", json.dumps(
        {"at": now if now is not None else time.time(), "workers": sorted(names)}))


def worker_heartbeat() -> dict | None:
    import json

    raw = get_flag("heartbeat", "")
    if not raw:
        return None
    try:
        return json.loads(raw) or None
    except Exception:
        return None


def workers_down(*, now: float | None = None,
                 stale_after: float = _HEARTBEAT_STALE_SECONDS) -> list[str]:
    """Which workers are NOT running.

    ``["daemon"]``  — nothing is beating at all: no daemon, or it died/wedged.
                      This is the bare-`python -m server` case.
    ``["evaluate"]``— the daemon is up but that loop is gone.
    ``[]``          — healthy.
    """
    import time

    hb = worker_heartbeat()
    if not hb:
        return ["daemon"]
    at = hb.get("at")
    clock = now if now is not None else time.time()
    if not isinstance(at, (int, float)) or clock - at > stale_after:
        return ["daemon"]
    alive = set(hb.get("workers") or [])
    return [w for w in _REQUIRED_WORKERS if w not in alive]


# --- reset epoch -------------------------------------------------------------
#
# Emptying the store does not stop the workers. A job that was already being
# scored or tailored finishes, writes itself back, and reappears on a board the
# owner just cleared. Each run carries the epoch it started in; a write from an
# earlier epoch is dropped.


def mark_reset() -> None:
    """Start a new epoch. Everything in flight belongs to the previous one."""
    import time

    set_flag("reset_epoch", str(int(time.time())))


def reset_epoch() -> str:
    return get_flag("reset_epoch") or "0"


def stale_run(started_epoch: str) -> bool:
    """True when this run began before the last reset, so its result is void."""
    return str(started_epoch or "0") != reset_epoch()
