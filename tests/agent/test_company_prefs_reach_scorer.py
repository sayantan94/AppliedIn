"""A company's preference overrides must reach the scorer, not only the screen.

The scorer's brief was built once, from the global file, when the graph was
imported. "US only" on Intel screened Intel's titles and then scored every Intel
posting against the global preferences, so a Bangalore role scored 8."""

from __future__ import annotations

import inspect

import agent.run as R
from core import flags
from discovery.watchlist import Preferences


def _row(company):
    return {"pk": f"{company.lower()}#1", "company": company, "jd_url": "https://x"}


def test_the_scorer_reads_its_brief_from_session_state():
    from agent import graph

    text = graph.scorer.instruction if isinstance(graph.scorer.instruction, str) else ""
    assert "{prefs_brief}" in text and "{prefs_notes}" in text


def test_a_company_override_shapes_the_brief(monkeypatch):
    monkeypatch.setattr(R, "_base_latex", lambda: "x")
    monkeypatch.setattr(R, "_github_context", lambda: "")
    monkeypatch.setattr(R, "_global_prefs", lambda: Preferences(locations=["WA", "CA"], notes="no clearance"))
    monkeypatch.setattr(flags, "company_pref",
                        lambda name: {"locations": ["US"], "notes": "US only, no relocation"}
                        if name.lower() == "intel" else {})
    intel = R._session_state(_row("Intel"), "jd")
    assert "US" in intel["prefs_brief"] and "WA" not in intel["prefs_brief"]
    assert intel["prefs_notes"] == "US only, no relocation"
    other = R._session_state(_row("Stripe"), "jd")
    assert "WA" in other["prefs_brief"]
    assert other["prefs_notes"] == "no clearance"


def test_locations_are_a_hard_rule_for_the_scorer():
    brief = R._prefs_brief(Preferences(locations=["US"]))
    assert "US" in brief
    assert "dealbreaker" in brief.lower() or "reject" in brief.lower()
