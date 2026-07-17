"""Public GitHub context for the candidate — repos, languages, topics.

Fed to the résumé tailor so it understands the candidate's real projects and
stack and can reword bullets toward a JD accurately (and surface a genuinely
relevant real project when it strengthens the match). Cached per process;
best-effort — returns '' on any failure so it never blocks tailoring.
"""

from __future__ import annotations

import re
from functools import lru_cache

from core.logging import get_logger

log = get_logger(__name__)


@lru_cache(maxsize=8)
def fetch_github_context(url: str, limit: int = 20) -> str:
    """A compact summary of the user's non-fork public repos, or '' on failure."""
    if not url:
        return ""
    m = re.search(r"github\.com/([A-Za-z0-9-]+)", url)
    if not m:
        return ""
    user = m.group(1)
    try:
        import httpx

        resp = httpx.get(
            f"https://api.github.com/users/{user}/repos",
            params={"sort": "pushed", "per_page": str(limit)},
            headers={"Accept": "application/vnd.github+json", "User-Agent": "AppliedIn"},
            timeout=15,
        )
        resp.raise_for_status()
        repos = resp.json()
    except Exception as exc:  # network / rate-limit / offline — never fatal
        log.warning("GitHub context fetch failed for %s: %s", user, exc)
        return ""

    lines = []
    for repo in repos:
        if not isinstance(repo, dict) or repo.get("fork"):
            continue
        bit = f"- {repo.get('name', '')}"
        if lang := repo.get("language"):
            bit += f" [{lang}]"
        if desc := (repo.get("description") or "").strip():
            bit += f": {desc}"
        if topics := ", ".join(repo.get("topics") or []):
            bit += f" (topics: {topics})"
        lines.append(bit)

    ctx = "\n".join(lines[:limit])
    if ctx:
        log.info("fetched %d GitHub repos for %s", len(lines), user)
    return ctx
