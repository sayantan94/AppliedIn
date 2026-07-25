"""Load and parse the repo-versioned config (watchlist + preferences).

These are bundled into the deploy artifact; changing them requires a redeploy
(deliberate non-scope of a runtime config store, per the HLD).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

from core.models import ApplyMode, DiscoveryMode


class CompanyConfig(BaseModel):
    name: str
    careers_url: str = ""  # the seed: a company's career-page URL
    # ats/board are DERIVED from careers_url by the resolver; set them only to
    # override detection. discovery is likewise resolved (feed vs crawl).
    ats: str = ""
    board: str = ""  # board URL or token, adapter-specific
    login_secret: str = ""  # Secrets Manager name for portal creds
    mode: ApplyMode = ApplyMode.GATED
    discovery: DiscoveryMode = DiscoveryMode.FEED
    requires_cover_letter: bool = False
    residential_proxy: bool = False

    @property
    def needs_resolution(self) -> bool:
        """True when ats/board must be derived from careers_url at runtime."""
        return not self.ats or (self.ats != "custom" and not self.board)


class Preferences(BaseModel):
    include_keywords: list[str] = []
    exclude_keywords: list[str] = []
    titles: list[str] = []
    locations: list[str] = []
    remote_only: bool = False
    seniority: list[str] = []
    min_match_score: int = 7
    # How many NEW postings one company run may hand to the pipeline. The
    # relevance screen returns them best-first, so this keeps a run to the top few
    # rather than tailoring dozens of marginal roles (a big board can match 85).
    # 0 = no cap.
    max_new_per_run: int = 5
    github: str = ""  # candidate's GitHub profile — extra context for tailoring
    notes: str = ""   # free-text hard constraints, read by BOTH the title screen
                      # and the JD scorer (e.g. no clearance, WA/CA only)


def load_watchlist(path: str | Path) -> list[CompanyConfig]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return [CompanyConfig(**c) for c in data.get("companies", [])]


def load_preferences(path: str | Path) -> Preferences:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return Preferences(**data)
