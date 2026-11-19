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
    # Ordinary ATS hints are resolver-derived. For a custom Greenhouse wrapper
    # only an operator-verified, repo-deployed explicit ats + board + careers_url
    # tuple is tenant authority; resolver guesses never grant it.
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


def load_effective_watchlist(path: str | Path, *, settings=None) -> list[CompanyConfig]:
    """Manual entries first; ONLY verified active funding rows extend local reads.

    This is a read-only overlay, not the tracked manual watchlist editor.
    A remote funding item cannot set application mode, secrets, proxy, discovery
    strategy, or authorization. Manual identities always take precedence.
    """
    manual = load_watchlist(path)
    if settings is None:
        from core.config import get_settings
        settings = get_settings()
    if settings.mode != "local" or not settings.funding_enabled:
        return manual

    from .funding import FundingRefusal, active_watchlist
    try:
        active = active_watchlist(settings)
    except (FundingRefusal, OSError, ValueError):
        # A broken local funding ledger cannot make auto companies eligible.
        # The importer independently refuses to overwrite that ledger.
        return manual

    claimed_names = {c.name.strip().casefold() for c in manual}
    claimed_domains: set[str] = set()
    merged = list(manual)
    for record in active:
        # These fields were revalidated by the strict private-state loader.
        # Aliases are evidence metadata only, never CompanyConfig authority.
        aliases = {a.strip().casefold() for a in record.get("aliases", [])}
        identities = {record["name"].strip().casefold()} | aliases
        domain = record["employer_domain"]
        if not identities or not identities.isdisjoint(claimed_names):
            continue
        if domain in claimed_domains:
            continue
        # Do not derive a manual employer domain from the shared ATS host in
        # careers_url. Without an independently proven manual homepage there
        # is no justified manual-domain collision to infer.
        merged.append(CompanyConfig(
            name=record["name"], careers_url=record["careers_url"],
            ats=record["ats"], board=record["board"],
            mode=ApplyMode.GATED, discovery=DiscoveryMode.FEED,
            login_secret="", residential_proxy=False,
            requires_cover_letter=False,
        ))
        claimed_names.update(identities)
        claimed_domains.add(domain)
    return merged
