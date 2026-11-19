"""Only a genuinely verified and fresh #386 pending no-opening row admits email.

All fixtures are synthetic, offline and write only to pytest tmp_path.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from discovery.funding import State
from core.outreach_sources import SourceHold, read_no_opening

NOW = datetime(2026, 11, 20, 8, tzinfo=UTC)
KEY = "a" * 32


def source(tmp_path, *, reason="no_openings", seen=NOW, enabled=True):
    s = SimpleNamespace(mode="local", local_dir=str(tmp_path),
                        outreach_enabled=enabled, funding_enabled=True)
    ledger = State(tmp_path / "funded-watchlist.yaml")
    ledger.data["pending"][KEY] = {
        "name": "Example Co", "homepage": "https://example-co.com",
        "employer_domain": "example-co.com", "aliases": [],
        "source": "synthetic", "reason": reason,
        "seen_at": seen.isoformat(),
        "next_check": (NOW + timedelta(days=1)).isoformat(),
        "expires": (NOW + timedelta(days=30)).isoformat(),
    }
    ledger.data["deals"][KEY] = {
        "status": "pending", "reason": reason, "source": "synthetic",
        "seen_at": seen.isoformat(),
    }
    ledger.save()
    return s


def test_no_opening_requires_exact_reason_and_default_off(tmp_path):
    s = source(tmp_path, enabled=False)
    with pytest.raises(SourceHold, match="off"):
        read_no_opening(s, KEY, now=NOW)
    s.outreach_enabled = True
    assert read_no_opening(s, KEY, now=NOW).domain == "example-co.com"
    for reason in ("guard_not_proven", "tenant_collision", "source_bound_refused"):
        source(tmp_path, reason=reason)
        with pytest.raises(SourceHold, match="not_proved"):
            read_no_opening(s, KEY, now=NOW)


def test_next_check_is_not_proof_of_successful_funding_recheck(tmp_path):
    s = source(tmp_path, seen=NOW - timedelta(days=8))
    with pytest.raises(SourceHold, match="fresh_successful"):
        read_no_opening(s, KEY, now=NOW)


def test_missing_or_colliding_identity_fails_closed(tmp_path):
    s = source(tmp_path)
    ledger = State(tmp_path / "funded-watchlist.yaml")
    ledger.data["pending"]["b"*32] = {
        "name": "Other Co", "homepage": "https://example-co.com",
        "employer_domain": "example-co.com", "aliases": [],
        "reason": "no_openings",
    }
    ledger.save()
    with pytest.raises(SourceHold, match="collision"):
        read_no_opening(s, KEY, now=NOW)
