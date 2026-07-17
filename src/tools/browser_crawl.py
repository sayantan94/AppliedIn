"""Crawl a JS-heavy / interaction-gated career page with browser-use.

Escalation fallback for the discovery crawler. The cheap path (headless render +
one-shot LLM extract) sees only the first paint, so client-rendered search UIs,
"Load more" pagination, location filters and infinite scroll (e.g.
jobs.apple.com/…/search) come back empty. When that happens, a real browser
agent (browser-use) drives the page — types the search, applies filters,
pages/scrolls — and returns the postings as STRUCTURED output (reliable, no
text-JSON parsing). Each browser step is streamed to the dashboard.

Read-only: the agent is told never to log in or apply.
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from core.events import emit
from core.logging import get_logger

log = get_logger(__name__)


class _Posting(BaseModel):
    job_id: str = ""
    title: str = ""
    url: str = ""


class _Postings(BaseModel):
    postings: list[_Posting] = []


async def crawl(url: str, company: str, search_terms: list[str], model: str) -> list[dict]:
    """Drive `url` with a browser agent, searching each of `search_terms`, and
    return the postings it finds, each a ``{"job_id", "title", "url"}`` dict
    (empty list if none / browser-use absent)."""
    try:
        from browser_use import Agent

        from tools.browser_llm import make_llm
    except Exception as exc:  # not installed
        log.error("browser-use not installed: %s (run ./setup.sh)", exc)
        return []

    terms = search_terms or ["software engineer"]
    terms_str = "; ".join(f'"{t}"' for t in terms)
    task = (
        f"Go to {url} — the careers / job-search page for {company}.\n"
        f"Run a SEARCH for EACH of these terms, one at a time: {terms_str}.\n"
        "For EACH term:\n"
        "  1. type the term into the page's SEARCH box and submit it,\n"
        "  2. extract the postings on the FIRST page of results (job_id from the URL, "
        "title, url),\n"
        "  3. then move on to the next term.\n"
        "Do NOT page beyond the first results page for a term, do NOT keep scrolling, "
        "do NOT log in or apply.\n"
        "When you've searched all the terms (or gathered ~25 postings total), FINISH and "
        "return the combined, de-duplicated postings as structured output — each with "
        "job_id (stable id from the URL), title, and url (direct link). Prefer finishing "
        "over gathering more."
    )
    pk = f"crawl#{company.strip().lower()}"
    emit("running", pk=pk, detail=f"crawling {company} careers (browser)", url=url)

    def _on_step(_state: object, output: object, n: int) -> None:  # streamed to the UI
        goal = (getattr(output, "next_goal", "") or "").strip()
        if goal:
            emit("response", pk=pk, agent="browser", detail=f"[step {n}] {goal}"[:240], url=url)

    agent = Agent(task=task, llm=make_llm(model), output_model_schema=_Postings,
                  register_new_step_callback=_on_step)
    history = await agent.run(max_steps=30)  # a few role searches + first-page extracts

    result = getattr(history, "structured_output", None)
    if isinstance(result, _Postings) and result.postings:
        found = [{"job_id": p.job_id, "title": p.title, "url": p.url}
                 for p in result.postings if p.job_id]
        log.info("%s: browser crawl returned %d postings (structured)", company, len(found))
        return found

    # Fallback: parse a JSON array out of the final text.
    text = (history.final_result() if hasattr(history, "final_result") else str(history)) or ""
    a, b = text.find("["), text.rfind("]")
    if a == -1 or b <= a:
        log.warning("%s: browser crawl returned no postings", company)
        return []
    try:
        items = json.loads(text[a : b + 1])
    except json.JSONDecodeError as exc:
        log.warning("%s: could not parse browser-crawl JSON: %s", company, exc)
        return []
    return [it for it in items if isinstance(it, dict) and it.get("job_id")]
