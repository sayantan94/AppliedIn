"""The screen's location rule must be the owner's, not a hardcoded pair of states.

Setting `locations: [US]` on a company produced a brief that said "REQUIRED: US.
Reject anything outside Washington or California" — two rules, one of them the
author's own, and the model followed whichever it liked."""

from __future__ import annotations

from discovery.relevance import _intent
from discovery.watchlist import Preferences


def test_the_location_rule_names_the_owners_locations():
    brief = _intent(Preferences(locations=["US"]))
    assert "US" in brief
    assert "Washington" not in brief and "California" not in brief


def test_no_locations_means_no_location_rule():
    assert "LOCATION" not in _intent(Preferences())


def test_remote_only_is_a_rule():
    assert "remote" in _intent(Preferences(remote_only=True)).lower()
