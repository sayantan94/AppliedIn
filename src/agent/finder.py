"""The finder agent — agentic discovery.

An ADK agent that finds new jobs across the watchlist and enqueues them for the
pipeline. Reliable feed parsing and dedup stay DETERMINISTIC (structured JSON
feeds don't need an LLM); the agent orchestrates and decides feed-vs-crawl,
using them as tools. This is the discovery half of the "everything is an agent"
model, kept honest about where the model actually adds value.
"""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.skills import load_skill_from_dir
from google.adk.tools import ToolContext
from google.adk.tools.skill_toolset import SkillToolset

from core.config import get_settings

from .graph import _SKILLS


def _model() -> LiteLlm:
    return LiteLlm(model=os.environ.get("APPLIEDIN_ADK_MODEL") or get_settings().litellm_model)


def list_companies(tool_context: ToolContext) -> dict:
    """List the watchlist companies to discover, with their career URLs."""
    from pathlib import Path

    from discovery.watchlist import load_watchlist

    path = Path(get_settings().config_dir) / "watchlist.yaml"
    return {"companies": [{"name": c.name, "careers_url": c.careers_url,
                           "discovery": c.discovery.value} for c in load_watchlist(path)]}


def discover_company(name: str, tool_context: ToolContext) -> dict:
    """Find + enqueue new jobs for ONE company: resolve its ATS from the careers
    URL, fetch the feed (or crawl a custom page), stage-1 filter, dedup, enqueue.
    Deterministic under the hood — returns how many new jobs were enqueued."""
    from pathlib import Path

    import httpx

    from core.models import DiscoveryMode
    from core.stores import make_stores
    from discovery.crawler import crawl_company
    from discovery.handler import discover_company as _feed_discover
    from discovery.handler import resolve_company
    from discovery.watchlist import load_preferences, load_watchlist

    cfg_dir = Path(get_settings().config_dir)
    prefs = load_preferences(cfg_dir / "preferences.yaml")
    companies = {c.name: c for c in load_watchlist(cfg_dir / "watchlist.yaml")}
    company = companies.get(name)
    if company is None:
        return {"error": f"unknown company {name!r}"}

    stores = make_stores()
    with httpx.Client(headers={"User-Agent": "AppliedIn/0.1"}) as client:
        company = resolve_company(company, client)
        if company.discovery is DiscoveryMode.FEED:
            n = _feed_discover(company, prefs, stores.tracking, stores.queue, client,
                               stores.tailor_queue)
        else:
            n = crawl_company(company, prefs, stores, client=client)
    return {"company": name, "enqueued": n}


root_agent = LlmAgent(
    name="finder", model=_model(),
    description="Discovers new job postings across the watchlist and enqueues "
    "matches for the application pipeline.",
    instruction=(
        "Find new jobs to apply to. Call list_companies to get the watchlist, then "
        "call discover_company(name) for EACH company. Report the total number of "
        "new jobs enqueued and any company that errored. Don't skip companies."
    ),
    tools=[SkillToolset(skills=[load_skill_from_dir(_SKILLS / "job-discovery")]),
           list_companies, discover_company],
)
