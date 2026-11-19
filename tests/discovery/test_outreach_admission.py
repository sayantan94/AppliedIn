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


def test_supported_general_ats_and_official_custom_cv_route_reuse_existing_identity(tmp_path):
    """Source tag is provenance, not relevance bypass or submission authorization."""
    from core.outreach_sources import verify_funded_application_route
    s = source(tmp_path)
    s.outreach_application_handoff_enabled = True
    accepted = verify_funded_application_route(
        s, KEY, {"ats":"ashby", "company":"Example Co", "job_id":"ashby:example-co:general"},
        "https://jobs.ashbyhq.com/example-co/general-123",
        {"title":"General Application", "text":"Source-verified public application details."},
        now=NOW)
    assert accepted.company_id == KEY
    custom = verify_funded_application_route(
        s, KEY, {"ats":"custom", "company":"Example Co", "job_id":"custom:123"},
        "https://example-co.com/careers/cv",
        {"title":"Talent Pool", "text":"Submit your CV through this official form."},
        now=NOW)
    assert custom.company_id == KEY
    # The same metadata cannot gain a different employer or an unsupported ATS.
    with pytest.raises(SourceHold, match="employer_mismatch"):
        verify_funded_application_route(
            s, KEY, {"ats":"ashby", "company":"Other Co"},
            "https://jobs.ashbyhq.com/other-co/general",
            {"title":"General Application", "text":"Apply here"}, now=NOW)
    with pytest.raises(SourceHold, match="not_supported"):
        verify_funded_application_route(
            s, KEY, {"ats":"unsupported", "company":"Example Co"},
            "https://unknown.example/job",
            {"title":"Open Application", "text":"Apply here"}, now=NOW)
    with pytest.raises(SourceHold, match="custom_cv_form_unverified"):
        verify_funded_application_route(
            s, KEY, {"ats":"custom", "company":"Example Co"},
            "https://third-party.example/forms/cv",
            {"title":"Talent Pool", "text":"Submit your CV"}, now=NOW)


def test_funded_general_route_default_off_and_no_relevance_exception(tmp_path):
    from core.outreach_sources import verify_funded_application_route
    s = source(tmp_path)
    record = {"ats":"ashby", "company":"Example Co"}
    meta = {"title":"General Application", "text":"A real posting."}
    with pytest.raises(SourceHold, match="handoff_off"):
        verify_funded_application_route(
            s, KEY, record, "https://jobs.ashbyhq.com/example-co/123", meta, now=NOW)
    s.outreach_application_handoff_enabled = True
    with pytest.raises(SourceHold, match="general_posting_not_proven"):
        verify_funded_application_route(
            s, KEY, record, "https://jobs.ashbyhq.com/example-co/123",
            {"title":"Senior Engineer", "text":"A real posting."}, now=NOW)
    with pytest.raises(SourceHold, match="general_posting_not_proven"):
        verify_funded_application_route(
            s, KEY, record, "https://jobs.ashbyhq.com/example-co/123",
            {"title":"General Application", "text":""}, now=NOW)
    s.mode = "cloud"
    with pytest.raises(SourceHold, match="handoff_off"):
        verify_funded_application_route(
            s, KEY, record, "https://jobs.ashbyhq.com/example-co/123", meta, now=NOW)
