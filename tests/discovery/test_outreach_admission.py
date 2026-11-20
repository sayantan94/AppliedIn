"""Only a genuinely verified and fresh #386 pending no-opening row admits email.

All fixtures are synthetic, offline and write only to pytest tmp_path.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from discovery import funding as funding_producer
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


def test_funded_real_apply_role_post_retains_normal_tracking_and_custom_lane(tmp_path, monkeypatch):
    """An in-process fake POST, not a live application or browser/SMTP call."""
    import sys
    from types import ModuleType
    from fastapi.testclient import TestClient
    import fakeredis
    import server
    from core.storage.local import RedisTracking
    from tools import jd, posting_guard as pg, submit_review

    # The in-process API uses wall-clock time; keep the synthetic witness
    # fresh without touching the owner's funding records.
    settings = source(tmp_path, seen=datetime.now(UTC) - timedelta(minutes=1))
    settings.outreach_application_handoff_enabled = True
    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    stores = SimpleNamespace(tracking=tracking)
    calls = []
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    monkeypatch.setattr(server, "make_stores", lambda _settings: stores)
    monkeypatch.setattr(submit_review, "recover_orphans", lambda: None)
    monkeypatch.setattr(server.submit_hold, "blocked", lambda *a, **kw: False)
    worker = ModuleType("agent.run")
    worker.run_job = lambda pk, _stores: calls.append(pk)
    events = ModuleType("core.events")
    events.emit = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "agent.run", worker)
    monkeypatch.setitem(sys.modules, "core.events", events)

    title = ["General Application"]
    def reader(url):
        text = ("This is a synthetic official employer posting. "
                "Submit your CV through this official form. ") * 8
        page = "<article>" + text + "</article>"
        inspected = pg.analyze(text, html_variants=[page],
                               source_required=True, source_complete=True)
        return {**inspected, "title": title[0],
                "source_kind": "ats_html", "source_url": url, "source_identity": {}}
    monkeypatch.setattr(jd, "fetch_jd_meta", reader)
    def post(client, url):
        return client.post("/actions/apply-role", json={
            "url":url, "discovery_source":"funded_no_opening", "funded_company_id":KEY
        }).json()

    with TestClient(server.create_app()) as client:
        supported = post(client, "https://jobs.ashbyhq.com/example-co/general-123")
        assert supported["ok"] and supported["status"] == "running"
        persisted = tracking.get(supported["pk"])
        assert persisted["company"] == "Example Co"
        assert persisted["ats"] == "ashby"
        assert persisted["discovery_source"] == "funded_no_opening"
        assert calls == [supported["pk"]]  # only the existing scorer/prepare workflow

        title[0] = "Talent Pool"
        official_form = post(client, "https://example-co.com/careers/cv")
        assert official_form["ok"] and official_form["status"] == "running"
        custom_row = tracking.get(official_form["pk"])
        assert custom_row["ats"] == "custom"
        assert custom_row["company"] == "Example Co"
        assert custom_row["discovery_source"] == "funded_no_opening"

        before = len(tracking.all())
        forged = post(client, "https://jobs.ashbyhq.com/other-co/general-123")
        assert not forged["ok"] and forged["error"] == "funded_application_employer_mismatch"
        title[0] = "Senior Software Engineer"
        irrelevant = post(client, "https://jobs.ashbyhq.com/example-co/engineering-123")
        assert not irrelevant["ok"] and irrelevant["error"] == "funded_general_posting_not_proven"
        assert len(tracking.all()) == before
        assert calls == [supported["pk"], official_form["pk"]]


def test_official_custom_cv_form_can_use_shorter_domain_label(tmp_path):
    from core.outreach_sources import verify_funded_application_route
    s = source(tmp_path)
    s.outreach_application_handoff_enabled = True
    row = State(tmp_path / "funded-watchlist.yaml")
    row.data["pending"][KEY]["name"] = "Example Co Incorporated"
    row.save()
    route = verify_funded_application_route(
        s, KEY, {"ats":"custom", "company":"Example Co", "job_id":"custom:abc"},
        "https://example-co.com/careers/cv",
        {"title":"Talent Pool", "text":"Please submit your CV via this official form."},
        now=NOW)
    assert route.name == "Example Co Incorporated"


def _due_pending_recheck(tmp_path, *, prior_success=None):
    """Synthetic due employer; no feeds, browser, mailbox or owner data."""
    settings = source(tmp_path, seen=NOW - timedelta(days=8))
    state = State(tmp_path / "funded-watchlist.yaml")
    row = state.data["pending"][KEY]
    row["next_check"] = (NOW - timedelta(minutes=1)).isoformat()
    if prior_success is not None:
        row["last_successful_no_opening_recheck_at"] = prior_success.isoformat()
    state.save()
    limits = SimpleNamespace(
        funding_max_candidates_per_pass=3,
        funding_daily_new_companies=20,
        funding_recheck_days=7,
    )
    return settings, state, limits


@pytest.mark.parametrize("reason", ["no_openings", "no_relevant_openings"])
def test_successful_no_opening_recheck_refreshes_proof_without_changing_seen_at(
        tmp_path, monkeypatch, reason):
    settings, state, limits = _due_pending_recheck(tmp_path)
    original_seen = state.data["pending"][KEY]["seen_at"]
    original_deal_seen = state.data["deals"][KEY]["seen_at"]
    monkeypatch.setattr(
        funding_producer, "_preview_verified",
        lambda *args, **kwargs: (reason, None))
    assert funding_producer._recheck_pending(
        state, limits, object(), NOW) == 1
    saved = State(tmp_path / "funded-watchlist.yaml").data
    row = saved["pending"][KEY]
    assert row["reason"] == reason
    assert saved["deals"][KEY]["reason"] == reason
    assert row["last_successful_no_opening_recheck_at"] == NOW.isoformat()
    assert row["seen_at"] == original_seen
    assert saved["deals"][KEY]["seen_at"] == original_deal_seen
    lead = read_no_opening(settings, KEY, now=NOW + timedelta(days=6))
    assert lead.verified_at == NOW


@pytest.mark.parametrize("prior_success", [None, NOW - timedelta(days=2)])
@pytest.mark.parametrize(
    "outcome",
    ["failed_read", "consumer_guard_refused", "relevance_budget",
     "daily_addition_deferred"],
)
def test_failed_or_partial_recheck_never_changes_success_proof(
        tmp_path, monkeypatch, prior_success, outcome):
    settings, state, limits = _due_pending_recheck(
        tmp_path, prior_success=prior_success)
    original_seen = state.data["pending"][KEY]["seen_at"]
    original_deal_seen = state.data["deals"][KEY]["seen_at"]

    if outcome == "failed_read":
        def preview(*args, **kwargs):
            raise funding_producer.FundingRefusal("synthetic_read_failure")
    elif outcome == "daily_addition_deferred":
        def preview(*args, **kwargs):
            return "admissible", {"name": "Example Co"}
        monkeypatch.setattr(state, "reserve", lambda *args, **kwargs: False)
    else:
        def preview(*args, **kwargs):
            return outcome, None

    monkeypatch.setattr(funding_producer, "_preview_verified", preview)
    assert funding_producer._recheck_pending(
        state, limits, object(), NOW) == 1
    saved = State(tmp_path / "funded-watchlist.yaml").data
    row = saved["pending"][KEY]
    assert row["reason"] == (
        "pending_recheck_failed" if outcome == "failed_read" else outcome)
    assert row["seen_at"] == original_seen
    assert saved["deals"][KEY]["seen_at"] == original_deal_seen
    if prior_success is None:
        assert "last_successful_no_opening_recheck_at" not in row
    else:
        assert row["last_successful_no_opening_recheck_at"] == prior_success.isoformat()
    with pytest.raises(SourceHold, match="not_proved_no_matching_opening"):
        read_no_opening(settings, KEY, now=NOW)


@pytest.mark.parametrize("age_days, permitted", [(6, True), (8, False)])
def test_absent_recheck_timestamp_uses_seen_at_and_holds_stale_sources(
        tmp_path, age_days, permitted):
    seen = NOW - timedelta(days=age_days)
    settings = source(tmp_path, seen=seen)
    row = State(tmp_path / "funded-watchlist.yaml").data["pending"][KEY]
    assert "last_successful_no_opening_recheck_at" not in row
    if permitted:
        assert read_no_opening(settings, KEY, now=NOW).verified_at == seen
    else:
        with pytest.raises(SourceHold, match="fresh_successful_funding_recheck_required"):
            read_no_opening(settings, KEY, now=NOW)


@pytest.mark.parametrize(
    "invalid_value, hold_reason",
    [("not-a-date", "invalid_funding_recheck_time"),
     (NOW.replace(tzinfo=None).isoformat(), "funding_recheck_timezone_missing"),
     (None, "missing_funding_recheck_time"),
     ((NOW + timedelta(days=1)).isoformat(), "funded_source_not_current")],
)
def test_present_invalid_recheck_witness_fails_closed_even_when_seen_at_fresh(
        tmp_path, invalid_value, hold_reason):
    settings = source(tmp_path)
    state = State(tmp_path / "funded-watchlist.yaml")
    state.data["pending"][KEY][
        "last_successful_no_opening_recheck_at"] = invalid_value
    state.save()
    for require_fresh in (True, False):
        with pytest.raises(SourceHold, match=hold_reason):
            read_no_opening(
                settings, KEY, now=NOW, require_fresh=require_fresh)
