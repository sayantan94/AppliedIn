"""A posting read during a SWEEP waits for a live application; one during an
APPLY does not.

Only "crawl" ever yielded, on the argument that a JD read is part of an
apply's own flow. That is true at apply time and false during the evaluate
sweep, which calls the same fetch in bulk: a Meta backlog of browser-only
postings would sit on the owner's one Chrome while a form was being filled.
So the kind is chosen by the caller — the sweep reads yield, the apply's own
read does not.
"""

from __future__ import annotations

import asyncio

import tools.claude_chrome as cc
from agent.run import _jd_text
from tools import jd


def test_sweep_reads_are_a_yielding_kind():
    assert "jd_sweep" in cc._YIELDS_TO_APPLY
    assert "jd" not in cc._YIELDS_TO_APPLY


def test_the_sweep_passes_the_yielding_kind(monkeypatch):
    seen = {}
    monkeypatch.setattr(jd, "fetch_jd", lambda url, kind="jd": seen.setdefault("kind", kind) or "")
    asyncio.run(_jd_text({"jd_url": "https://x/1", "jd_text": ""}))
    assert seen["kind"] == "jd_sweep"


def test_the_apply_passes_the_non_yielding_kind(monkeypatch):
    seen = {}
    monkeypatch.setattr(jd, "fetch_jd", lambda url, kind="jd": seen.setdefault("kind", kind) or "")
    asyncio.run(_jd_text({"jd_url": "https://x/1", "jd_text": ""}, yielding=False))
    assert seen["kind"] == "jd"
