"""Offline synthetic contract checks for the default-off funding lane (#386).

No network, owner files, browser, Redis, email, applications or credentials.
CI/operator is responsible for execution; these are NOT live-source checks.
"""
from __future__ import annotations

import json
import re
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from discovery import funding
from discovery.funding import load_effective_watchlist

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "funding"
NOW = datetime(2026, 11, 19, 12, tzinfo=UTC)
SOURCE = "https://feeds.example.com/rss"
ARTICLE = "https://news.example.com/round-1"
HOME = "https://example-co.co.uk/"
BOARD = "https://jobs.ashbyhq.com/example-co"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/example-co?includeCompensation=true"
SEC_INDEX = "https://www.sec.gov/Archives/edgar/data/1/000000-index.html"
SEC_DOC = "https://www.sec.gov/Archives/edgar/data/1/primary_doc.xml"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def settings(tmp_path, *, enabled=True, source=True, mode="local"):
    manifest = {
        "version": 1,
        "regions": ["eu", "uk", "usa", "uae", "gcc", "india",
                    "singapore", "australia", "remote-first-anywhere"],
        "sources": [
            {"id": "uk_feed", "kind": "rss", "region": "uk",
             "url": SOURCE, "enabled": source},
        ],
    }
    (tmp_path / "funding-feeds.yaml").write_text(yaml.safe_dump(manifest))
    (tmp_path / "watchlist.yaml").write_text("companies: []\n")
    (tmp_path / "preferences.yaml").write_text("titles: [Software Engineer]\n")
    return SimpleNamespace(
        mode=mode, funding_enabled=enabled, funding_poll_hours=5,
        funding_sec_user_agent="", funding_eligible_days=30,
        funding_daily_extraction_calls=8,
        funding_weekly_extraction_tokens=100000,
        funding_extract_max_output_tokens=1200,
        funding_daily_new_companies=10,
        funding_daily_http=120, funding_max_http_per_pass=60,
        funding_max_candidates_per_pass=20,
        funding_max_postings_per_pass=12,
        funding_daily_relevance_calls=12,
        funding_recheck_days=7,
        funding_pending_days=60,
        funding_active_days=90,
        config_dir=str(tmp_path), local_dir=str(tmp_path / "local"),
    )


class OfflineClient:
    """A deterministic response lookup, not an httpx network transport."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, *, params=None, headers=None, follow_redirects=False, **_):
        from urllib.parse import urlencode

        if params:
            url = url + ("&" if "?" in url else "?") + urlencode(params)
        self.calls.append((url, dict(headers or {}), follow_redirects))
        response = self.routes.get(url)
        if response is None:
            raise funding.FundingRefusal("synthetic_route_missing")
        if isinstance(response, tuple):
            status, data = response
        else:
            status, data = 200, response
        if isinstance(data, str):
            data = data.encode("utf-8")
        return httpx.Response(
            status_code=status, content=data,
            headers={"ETag": '"synthetic-v1"'},
            request=httpx.Request("GET", url),
        )


def routes(keep_opening=True):
    mapping = {
        SOURCE: fixture("rss_uk.xml"),
        ARTICLE: fixture("article_uk.html"),
        HOME: fixture("employer_uk.html"),
        BOARD: fixture("board_ashby.html"),
        ASHBY_API: fixture("ashby.json"),
    }
    if not keep_opening:
        mapping[ASHBY_API] = b'{"jobs":[]}'
    return mapping


def extractor(prompt, *, max_tokens):
    assert 0 < max_tokens <= 1200
    ids = re.findall(r'"id":"([0-9a-f]{32})"', prompt)
    return {"articles": [{"id": id_, "deals": [{
        "issuer": "example-co", "stage": "seed", "date": "2026-11-18",
        "sector": "fintech", "region": "uk", "remote_first": False,
        "amount": "USD 2M", "aliases": [],
    }]} for id_ in ids]}


def test_source_and_poller_are_default_off(tmp_path, monkeypatch):
    monkeypatch.delenv("APPLIEDIN_DISCOVERY", raising=False)
    config = settings(tmp_path, enabled=False)
    assert not funding.ready(config)
    assert funding.poll_once(config) == {"status": "disabled"}
    assert not (tmp_path / "local").exists()
    config.funding_enabled = True
    config.mode = "cloud"
    assert not funding.ready(config)
    assert funding.active_watchlist(config) == []
    config.mode = "local"
    config.funding_enabled = False
    assert load_effective_watchlist(tmp_path / "watchlist.yaml", settings=config) == []
    config.funding_enabled = True
    (tmp_path / "funding-feeds.yaml").write_text(
        (tmp_path / "funding-feeds.yaml").read_text().replace(
            "enabled: true", "enabled: false"))
    assert not funding.ready(config)


def test_missing_auto_overlay_preserves_injected_manual_loader(tmp_path):
    cfg = settings(tmp_path, enabled=False)
    path = tmp_path / "missing-manual.yaml"
    from discovery.watchlist import CompanyConfig

    manual = [CompanyConfig(name="TestCo", careers_url="https://example.com/jobs")]
    seen = []

    def manual_loader(candidate):
        seen.append(candidate)
        return manual

    # The caller's injected loader owns manual reads, whether funding is off
    # or is on with no private overlay file.
    assert load_effective_watchlist(path, settings=cfg, manual_loader=manual_loader) == manual
    cfg.funding_enabled = True
    assert load_effective_watchlist(path, settings=cfg, manual_loader=manual_loader) == manual
    assert seen == [path, path]
    assert not (tmp_path / "local").exists()


def test_live_status_is_opt_in_but_all_candidate_fixtures_are_offline(tmp_path):
    config = settings(tmp_path, source=False)
    assert not funding.ready(config)
    config.funding_sec_user_agent = ""
    assert funding._manifest(config)["sources"][0]["enabled"] is False
    assert not (tmp_path / "local").exists()


def test_native_ashby_requires_current_producer_consumer_and_relevance(tmp_path, monkeypatch):
    monkeypatch.delenv("APPLIEDIN_DISCOVERY", raising=False)
    cfg = settings(tmp_path)
    client = OfflineClient(routes())
    result = funding.poll_once(
        cfg, now=NOW, client_factory=lambda *_: client,
        extractor=extractor, relevance_fn=lambda jobs, prefs: jobs,
    )
    state = funding.State(tmp_path / "local" / "funded-watchlist.yaml")
    assert result["status"] == "ok"
    assert len(state.data["active"]) == 1
    record = next(iter(state.data["active"].values()))
    assert record["name"] == "example-co"
    assert record["careers_url"] == BOARD
    assert record["mode"] == "gated" and record["discovery"] == "feed"
    assert record["employer_domain"] == "example-co.co.uk"
    assert "login_secret" not in record
    assert len(load_effective_watchlist(tmp_path / "watchlist.yaml", settings=cfg)) == 1
    assert all("http://127." not in call[0] for call in client.calls)
    assert state.data["report"]["counts"]["admissible"] == 1


def test_manual_name_precedence_is_casefolded(tmp_path):
    cfg = settings(tmp_path)
    (tmp_path / "watchlist.yaml").write_text(
        "companies:\n- name: EXAMPLE-CO\n  careers_url: https://jobs.ashbyhq.com/manual-board\n"
    )
    state = funding.State(tmp_path / "local" / "funded-watchlist.yaml")
    state.data["active"]["synthetic-deal"] = {
        "name": "example-co", "careers_url": BOARD, "ats": "ashby",
        "board": "example-co", "mode": "gated", "discovery": "feed",
        "employer_domain": "example-co.co.uk", "homepage": HOME,
        "aliases": [], "source_id": "uk_feed",
        "admitted_at": NOW.isoformat(), "last_relevant_at": NOW.isoformat(),
    }
    state.save()
    effective = load_effective_watchlist(tmp_path / "watchlist.yaml", settings=cfg)
    assert len(effective) == 1 and effective[0].name == "EXAMPLE-CO"


def test_hosted_ats_host_does_not_identify_employers(tmp_path):
    cfg = settings(tmp_path)
    state = funding.State(tmp_path / "local" / "funded-watchlist.yaml")
    base = {
        "careers_url": BOARD, "ats": "ashby",
        "board": "example-co", "mode": "gated", "discovery": "feed",
        "employer_domain": "example-co.co.uk", "homepage": HOME,
        "aliases": [], "source_id": "uk_feed",
        "admitted_at": NOW.isoformat(), "last_relevant_at": NOW.isoformat(),
    }
    state.data["active"]["first"] = {**base, "name": "example-co"}
    state.data["active"]["second"] = {
        **base, "name": "OtherCo", "board": "otherco",
        "careers_url": "https://jobs.ashbyhq.com/otherco",
        "homepage": "https://otherco.com",
        "employer_domain": "otherco.com",
    }
    state.save()
    merged = load_effective_watchlist(tmp_path / "watchlist.yaml", settings=cfg)
    assert {x.name for x in merged} == {"example-co", "OtherCo"}


def test_absent_or_corrupt_private_state_fails_closed(tmp_path):
    cfg = settings(tmp_path)
    path = tmp_path / "local" / "funded-watchlist.yaml"
    assert funding.active_watchlist(cfg) == []
    path.parent.mkdir(parents=True)
    path.write_text("version: this is not state\n")
    before = path.read_bytes()
    with pytest.raises(funding.FundingRefusal):
        funding.State(path)
    assert path.read_bytes() == before
    assert load_effective_watchlist(tmp_path / "watchlist.yaml", settings=cfg) == []


def test_auto_record_must_not_import_authority_fields(tmp_path):
    path = tmp_path / "state.yaml"
    state = funding.State(path)
    state.data["active"]["evil"] = {
        "name": "example-co", "careers_url": BOARD, "ats": "ashby",
        "board": "example-co", "mode": "auto", "discovery": "feed",
        "employer_domain": "example-co.co.uk", "homepage": HOME,
        "aliases": [], "source_id": "uk_feed",
        "admitted_at": NOW.isoformat(), "last_relevant_at": NOW.isoformat(),
        "login_secret": "placeholder-secret",
    }
    with pytest.raises(funding.FundingRefusal):
        state.save()
    assert not path.exists()


def test_atomic_save_failure_keeps_existing_valid_state(tmp_path, monkeypatch):
    path = tmp_path / "funded-watchlist.yaml"
    ledger = funding.State(path)
    ledger.save()
    before = path.read_bytes()
    ledger.data["deals"]["test"] = {"status": "retryable", "seen_at": NOW.isoformat()}
    def explode(*_args, **_kwargs):
        raise OSError("injected replace failure")
    monkeypatch.setattr(funding.os, "replace", explode)
    with pytest.raises(OSError):
        ledger.save()
    assert path.read_bytes() == before
    assert funding.State(path).data["deals"] == {}


def test_worst_case_model_budget_persists_through_crash(tmp_path):
    cfg = settings(tmp_path)
    cfg.funding_daily_extraction_calls = 1
    path = tmp_path / "funded-watchlist.yaml"
    state = funding.State(path)
    article = {"key": "synthetic", "title": "Seed round",
               "text": "London fintech company raises seed funding"}
    def crash(*_args, **_kwargs):
        raise RuntimeError("synthetic model crash")
    with pytest.raises(RuntimeError):
        funding._extract_batch([article], state, cfg, NOW, extractor=crash)
    restarted = funding.State(path)
    assert restarted.data["budgets"]["day_calls"] == 1
    assert restarted.data["budgets"]["week_tokens"] > 0
    with pytest.raises(funding.FundingRefusal):
        funding._extract_batch([article], restarted, cfg, NOW, extractor=crash)


def test_no_relevant_opening_stays_pending_for_weekly_recheck(tmp_path, monkeypatch):
    monkeypatch.delenv("APPLIEDIN_DISCOVERY", raising=False)
    cfg = settings(tmp_path)
    client = OfflineClient(routes(keep_opening=False))
    funding.poll_once(cfg, now=NOW, client_factory=lambda *_: client,
                      extractor=extractor, relevance_fn=lambda jobs, prefs: jobs)
    state = funding.State(tmp_path / "local" / "funded-watchlist.yaml")
    assert not state.data["active"]
    assert len(state.data["pending"]) == 1
    pending = next(iter(state.data["pending"].values()))
    assert pending["employer_domain"] == "example-co.co.uk"
    assert funding._date(pending["next_check"]) >= NOW + timedelta(days=6)
    cfg.funding_enabled = False
    assert funding.active_watchlist(cfg) == []


def test_due_pending_promotes_only_after_fresh_source_and_consumer_proof(tmp_path):
    cfg = settings(tmp_path)
    state = funding.State(tmp_path / "local" / "funded-watchlist.yaml")
    key = "synthetic-verified-pending"
    state.data["pending"][key] = {
        "name": "example-co", "homepage": HOME,
        "employer_domain": "example-co.co.uk", "source": "uk_feed",
        "reason": "no_openings",
        "seen_at": (NOW - timedelta(days=7)).isoformat(),
        "next_check": (NOW - timedelta(hours=1)).isoformat(),
        "expires": (NOW + timedelta(days=53)).isoformat(),
    }
    state.data["deals"][key] = {
        "status": "pending", "reason": "no_openings", "source": "uk_feed",
        "seen_at": NOW.isoformat(),
    }
    state.save()
    client = OfflineClient(routes())
    funding._recheck_pending(
        state, cfg, client, NOW, relevance_fn=lambda jobs, prefs: jobs,
    )
    assert key in state.data["active"]
    assert key not in state.data["pending"]
    assert state.data["deals"][key]["status"] == "active"


def test_greenhouse_wrapper_remains_guard_not_proven(tmp_path, monkeypatch):
    monkeypatch.delenv("APPLIEDIN_DISCOVERY", raising=False)
    cfg = settings(tmp_path)
    fake = routes()
    fake[HOME] = b"""<html><body><h1>example-co</h1> fintech careers
    <a href="https://job-boards.greenhouse.io/example-co">Jobs</a></body></html>"""
    client = OfflineClient(fake)
    funding.poll_once(cfg, now=NOW, client_factory=lambda *_: client,
                      extractor=extractor, relevance_fn=lambda jobs, prefs: jobs)
    state = funding.State(tmp_path / "local" / "funded-watchlist.yaml")
    assert not state.data["active"]
    assert len(state.data["pending"]) == 1
    assert next(iter(state.data["pending"].values()))["reason"] == "guard_not_proven"


def test_source_304_and_partial_replay_cannot_hide_retryable_article(tmp_path):
    cfg = settings(tmp_path)
    state = funding.State(tmp_path / "funded-watchlist.yaml")
    state.data["feeds"]["uk_feed"] = {"etag": '"synthetic-v1"', "last_modified": ""}
    state.data["articles"]["retry"] = {"status": "retryable", "source": "uk_feed",
                                          "seen_at": NOW.isoformat()}
    state.save()
    client = OfflineClient({SOURCE: fixture("rss_uk.xml")})
    items, status = funding._source_payload(
        funding._manifest(cfg)["sources"][0], state, client, NOW)
    assert items and status == "fetched"
    assert not client.calls[0][1], "Unfinished feeds must bypass stale ETag 304"
    no_work = OfflineClient({SOURCE: (304, b"")})
    # Completed articles no longer need replay. A missing article record
    # is deliberately NOT treated as complete: that could lose an item.
    for item in items:
        state.data["articles"][item["key"]]["status"] = "complete"
    state.data["articles"]["retry"]["status"] = "complete"
    state.save()
    items, status = funding._source_payload(
        funding._manifest(cfg)["sources"][0], state, no_work, NOW)
    assert items == [] and status == "not_modified"


def test_multi_deal_plan_persists_first_child_even_when_second_retries(tmp_path, monkeypatch):
    monkeypatch.delenv("APPLIEDIN_DISCOVERY", raising=False)
    cfg = settings(tmp_path)
    fake = routes()
    fake[SOURCE] = fixture("rss_multi.xml")
    fake["https://news.example.com/two-deals"] = fixture("article_multi.html")
    client = OfflineClient(fake)
    seen_extractions = []
    def multi(prompt, *, max_tokens):
        seen_extractions.append(1)
        ids = re.findall(r'"id":"([0-9a-f]{32})"', prompt)
        return {"articles": [{"id": k, "deals": [
            {"issuer": name, "stage": "seed", "date": "2026-11-18",
             "sector": "fintech", "region": "uk", "remote_first": False}
            for name in ("example-co", "OtherCo")
        ]} for k in ids]}
    funding.poll_once(cfg, now=NOW, client_factory=lambda *_: client,
                      extractor=multi, relevance_fn=lambda jobs, prefs: jobs)
    state = funding.State(tmp_path / "local" / "funded-watchlist.yaml")
    assert len(state.data["active"]) == 1
    assert len(state.data["deals"]) == 2
    article = next(iter(state.data["articles"].values()))
    assert article["status"] == "retryable" and len(article["children"]) == 2
    funding.poll_once(cfg, now=NOW, client_factory=lambda *_: client,
                      extractor=lambda *_args, **_kwargs: pytest.fail("re-extracted persisted plan"),
                      relevance_fn=lambda jobs, prefs: jobs)
    assert len(seen_extractions) == 1
    assert len(funding.State(tmp_path / "local" / "funded-watchlist.yaml").data["active"]) == 1


def test_read_only_legacy_guards_are_reused(tmp_path):
    from core.models import JobRecord
    from discovery.canonical_job_url import admit_feed, source_bound
    from discovery.watchlist import CompanyConfig

    co = CompanyConfig(name="example-co", careers_url=BOARD, ats="ashby", board="example-co")
    job = JobRecord(
        company="example-co", ats="ashby", job_id="123e4567-e89b-42d3-a456-426614174000",
        title="Software Engineer", jd_url=BOARD + "/123e4567-e89b-42d3-a456-426614174000",
        jd_text="Synthetic posting",
    )
    assert source_bound(co, co)
    accepted, refused = admit_feed(co, co, [job])
    assert len(accepted) == 1 and not refused
    wrapper = CompanyConfig(name="example-co",
                            careers_url="https://example-co.co.uk/careers")
    rejected, refused = admit_feed(co, wrapper, [job])
    assert not rejected and refused


def test_psl_bundled_offline_and_unknown_suffix_fails_closed():
    assert funding.registrable("https://jobs.example-co.co.uk") == "example-co.co.uk"
    assert funding.registrable("https://example-co.com") == "example-co.com"
    assert funding.registrable("https://example-co.unknownsuffix") is None
    assert funding.registrable("http://example-co.com") is None


@pytest.mark.parametrize("url", [
    "http://example.com", "https://test@other.com/jobs",
    "https://localhost/jobs", "https://example.com:8443/jobs",
    "https://example.com/%00", "https://example.com/../private",
])
def test_unsafe_origins_cannot_provide_authority(url):
    if url == "https://example.com/%00" or url == "https://example.com/../private":
        # URL path validation belongs to the canonical posting guard; connect
        # authority validation here guarantees HTTPS host and port.
        assert funding._safe_url(url)[0] == "example.com"
    else:
        with pytest.raises(funding.FundingRefusal):
            funding._safe_url(url)


def test_connect_time_dns_rebinding_refuses_all_private_addresses(monkeypatch):
    calls = []
    def changing_dns(host, port, *, type):
        calls.append(host)
        ip = "93.184.216.34" if len(calls) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    monkeypatch.setattr(funding.socket, "getaddrinfo", changing_dns)
    assert funding._public_addresses("example.com", 443)
    with pytest.raises(funding.FundingRefusal, match="nonpublic_connect_address"):
        funding._SafeTLS("example.com", timeout=1).connect()
    assert len(calls) == 2


def test_sec_index_only_primary_doc_and_no_filing_person_data(tmp_path):
    state = funding.State(tmp_path / "funded-watchlist.yaml")
    cfg = settings(tmp_path)
    client = OfflineClient({
        SEC_INDEX: fixture("sec_index.html"),
        SEC_DOC: fixture("sec_primary_doc.xml"),
    })
    source = {"id": "sec_test"}
    article = {"key": "synthetic-sec", "url": SEC_INDEX}
    funding._sec_proxy(source, article, state, client, NOW)
    row = next(iter(state.data["deals"].values()))
    assert row["status"] == "unresolved"
    assert row["reason"] == "sec_unverified_offering_proxy"
    assert row["offering_amount_proxy"] == "3000000"
    assert "officer" not in yaml.safe_dump(state.data).lower()
    assert "user-agent" not in yaml.safe_dump(state.data).lower()
    bad = OfflineClient({SEC_DOC: fixture("sec_pooled.xml")})
    funding._sec_proxy(source, {"key": "other", "url": SEC_DOC}, state, bad, NOW)
    assert sorted(x["status"] for x in state.data["deals"].values()) == ["rejected", "unresolved"]


def test_yc_cohort_is_not_funding_round_evidence(tmp_path):
    cfg = settings(tmp_path)
    item = funding._manifest(cfg)["sources"][0]
    item = {**item, "kind": "yc_json"}
    client = OfflineClient({SOURCE: fixture("yc.json")})
    state = funding.State(tmp_path / "funded-watchlist.yaml")
    records, status = funding._source_payload(item, state, client, NOW)
    assert records == [] and status == "yc_not_round_evidence"


def test_eligible_region_and_round_requires_independent_evidence():
    assert funding._regions("fintech in London", {"uk"}) == {"uk"}
    assert funding._regions("unknown", {"uk"}) == set()
    assert funding._regions("remote-first", {"remote-first-anywhere"}) == {
        "remote-first-anywhere"}
    assert funding._fresh("2026-11-18", NOW, 30)
    assert not funding._fresh("2025-02-18", NOW, 30)
    assert not funding._fresh("2030-02-18", NOW, 30)


def test_manual_native_board_wins_even_when_auto_name_differs(tmp_path):
    cfg = settings(tmp_path)
    (tmp_path / "watchlist.yaml").write_text(
        "companies:\n- name: ManualCo\n  careers_url: " + BOARD + "\n"
    )
    state = funding.State(tmp_path / "local" / "funded-watchlist.yaml")
    state.data["active"]["other"] = {
        "name": "OtherCo", "careers_url": BOARD, "ats": "ashby",
        "board": "example-co", "mode": "gated", "discovery": "feed",
        "employer_domain": "otherco.com", "homepage": "https://otherco.com",
        "aliases": [], "source_id": "uk_feed", "admitted_at": NOW.isoformat(),
        "last_relevant_at": NOW.isoformat(),
    }
    state.save()
    merged = load_effective_watchlist(tmp_path / "watchlist.yaml", settings=cfg)
    assert [x.name for x in merged] == ["ManualCo"], "a tenant is not shared authority"


def test_exact_tenant_collision_refuses_different_employer(tmp_path):
    cfg = settings(tmp_path)
    ledger = funding.State(tmp_path / "local" / "funded-watchlist.yaml")
    ledger.data["active"]["first"] = {
        "name": "OtherCo", "careers_url": BOARD, "ats": "ashby",
        "board": "example-co", "mode": "gated", "discovery": "feed",
        "employer_domain": "otherco.com", "homepage": "https://otherco.com",
        "aliases": [], "source_id": "uk_feed", "admitted_at": NOW.isoformat(),
        "last_relevant_at": NOW.isoformat(),
    }
    ledger.save()
    reason, active = funding._preview_verified(
        "example-co", HOME, "uk_feed", ledger, cfg,
        OfflineClient(routes()), NOW, relevance_fn=lambda jobs, prefs: jobs)
    assert (reason, active) == ("tenant_collision", None)


def test_board_redirect_to_another_tenant_is_not_owner_evidence(tmp_path):
    cfg = settings(tmp_path)
    ledger = funding.State(tmp_path / "local" / "funded-watchlist.yaml")

    class RedirectedBoard(OfflineClient):
        def get(self, url, **kwargs):
            reply = super().get(url, **kwargs)
            if url == BOARD:
                return httpx.Response(
                    200, content=reply.content,
                    request=httpx.Request(
                        "GET", "https://jobs.ashbyhq.com/foreign-tenant"),
                )
            return reply

    reason, active = funding._preview_verified(
        "example-co", HOME, "uk_feed", ledger, cfg,
        RedirectedBoard(routes()), NOW, relevance_fn=lambda jobs, prefs: jobs)
    assert (reason, active) == ("guard_not_proven", None)


def test_alias_needs_two_independent_sources_and_manual_wins(tmp_path):
    cfg = settings(tmp_path)
    (tmp_path / "watchlist.yaml").write_text(
        "companies:\n- name: LEGACYCO\n  careers_url: https://jobs.lever.co/legacyco\n"
    )
    news = funding._article(
        '<h1>example-co (formerly LegacyCo) raised seed fintech funding in London.</h1>'
        '<a href="https://example-co.co.uk/">example-co</a>')
    home = b'<h1>example-co, formerly LegacyCo</h1><p>Fintech in London.</p>'
    article = {
        "source": "uk_feed", "date": "2026-11-18",
        "text": news.text, "html": news, "url": ARTICLE,
    }
    deal = {
        "issuer": "example-co", "stage": "seed", "date": "2026-11-18",
        "sector": "fintech", "region": "uk", "aliases": ["LegacyCo"],
    }
    reason, active, evidence = funding._prove_deal(
        deal, article, funding.State(tmp_path / "local" / "funded-watchlist.yaml"),
        cfg, OfflineClient({HOME: home}), NOW,
        relevance_fn=lambda jobs, prefs: jobs)
    assert reason == "manual_precedence" and active is None
    assert evidence["aliases"] == ["LegacyCo"]
    # An alias asserted only by a publisher is not automatically trusted.
    reason, active, evidence = funding._prove_deal(
        deal, article, funding.State(tmp_path / "local" / "funded-watchlist.yaml"),
        cfg, OfflineClient({HOME: b"<h1>example-co</h1><p>London fintech.</p>",
                            BOARD: fixture("board_ashby.html"), ASHBY_API: fixture("ashby.json")}),
        NOW, relevance_fn=lambda jobs, prefs: jobs)
    assert evidence["aliases"] == []
    assert reason != "manual_precedence"


def test_other_deals_stage_cannot_authorize_this_issuer(tmp_path):
    cfg = settings(tmp_path)
    news = funding._article(
        '<p>example-co received an industry award.</p>'
        '<p>OtherCo announced seed fintech financing in London.</p>')
    deal = {"issuer": "example-co", "stage": "seed",
            "date": "2026-11-18", "sector": "fintech", "region": "uk"}
    reason, active, evidence = funding._prove_deal(
        deal, {"source": "uk_feed", "date": "2026-11-18",
               "text": news.text, "html": news, "url": ARTICLE},
        funding.State(tmp_path / "local" / "funded-watchlist.yaml"),
        cfg, OfflineClient({}), NOW)
    assert (reason, active, evidence) == ("round_unconfirmed", None, None)


def test_old_retryables_compact_without_resetting_open_spend(tmp_path):
    path = tmp_path / "funded-watchlist.yaml"
    state = funding.State(path)
    state.data["deals"]["stale-retry"] = {
        "status": "retryable",
        "seen_at": (NOW - timedelta(days=90)).isoformat(),
    }
    state.data["articles"]["stale-defer"] = {
        "status": "deferred", "source": "uk_feed",
        "seen_at": (NOW - timedelta(days=90)).isoformat(),
    }
    assert state.reserve(NOW, "day_calls", 1, 8)
    assert state.reserve(NOW, "week_tokens", 3000, 10000)
    state.compact(NOW)
    reloaded = funding.State(path).data
    assert reloaded["deals"] == {} and reloaded["articles"] == {}
    assert reloaded["budgets"]["day_calls"] == 1
    assert reloaded["budgets"]["week_tokens"] == 3000


def test_invalid_source_and_local_schema_fail_closed(tmp_path):
    cfg = settings(tmp_path)
    manifest = yaml.safe_load((tmp_path / "funding-feeds.yaml").read_text())
    manifest["regions"].append(["injected-invalid-type"])
    (tmp_path / "funding-feeds.yaml").write_text(yaml.safe_dump(manifest))
    assert not funding.ready(cfg)
    path = tmp_path / "funded-watchlist.yaml"
    state = funding.State(path)
    state.data["budgets"]["day"] = ["malformed-period"]
    with pytest.raises(funding.FundingRefusal, match="corrupt_budget"):
        state.save()
    state.data["budgets"]["day"] = "2026-11-19"
    state.data["active"]["bad"] = {
        "name": "example-co", "careers_url": BOARD, "ats": [],
        "board": "example-co", "mode": "gated", "discovery": "feed",
        "employer_domain": "example-co.co.uk", "homepage": HOME,
        "aliases": [], "source_id": "uk_feed",
        "admitted_at": NOW.isoformat(), "last_relevant_at": NOW.isoformat(),
    }
    with pytest.raises(funding.FundingRefusal, match="auto_record_authority"):
        state.save()


def test_resolver_probe_uses_guarded_connect_time_dns(tmp_path, monkeypatch):
    from discovery.resolver import resolve

    cfg = settings(tmp_path)
    ledger = funding.State(tmp_path / "funded-watchlist.yaml")
    calls = []
    def private_dns(host, port, *, type):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("127.0.0.1", port))]
    monkeypatch.setattr(funding.socket, "getaddrinfo", private_dns)
    client = funding.GuardedClient(state=ledger, settings=cfg, now=NOW)
    with pytest.raises(funding.FundingRefusal, match="nonpublic_connect_address"):
        resolve("https://example-co.co.uk/careers", client=client, name="example-co")
    assert calls == ["example-co.co.uk"]
    assert ledger.data["budgets"]["day_http"] == 1


def test_guarded_redirect_strips_auth_and_cross_host_conditionals(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    ledger = funding.State(tmp_path / "funded-watchlist.yaml")
    requests = []

    class SyntheticResponse:
        def __init__(self, status, headers, content):
            self.status, self.headers, self.content = status, headers, content
        def read(self, _limit):
            return self.content
        def getheaders(self):
            return list(self.headers.items())

    class SyntheticTLS:
        def __init__(self, host, _port, **_kwargs):
            self.host = host
        def request(self, method, path, *, headers):
            requests.append((self.host, method, path, dict(headers)))
        def getresponse(self):
            if self.host == "feeds.example.com":
                return SyntheticResponse(
                    302, {"Location": "https://other.example.com/new"}, b"")
            return SyntheticResponse(200, {}, b"synthetic")
        def close(self):
            pass

    monkeypatch.setattr(funding, "_SafeTLS", SyntheticTLS)
    client = funding.GuardedClient(state=ledger, settings=cfg, now=NOW)
    response = client.get(
        "https://feeds.example.com/item", follow_redirects=True,
        headers={"Authorization": "no-forward", "Cookie": "no-forward",
                 "If-None-Match": '"local-etag"'})
    assert response.content == b"synthetic"
    assert [x[0] for x in requests] == ["feeds.example.com", "other.example.com"]
    assert requests[0][3].get("If-None-Match") == '"local-etag"'
    assert "If-None-Match" not in requests[1][3]
    assert all("Authorization" not in x[3] and "Cookie" not in x[3]
               for x in requests)
    assert ledger.data["budgets"]["day_http"] == 2


def test_daily_additions_defer_until_next_pass_and_unproductive_active_expires(tmp_path):
    cfg = settings(tmp_path)
    cfg.funding_daily_new_companies = 0
    funding.poll_once(
        cfg, now=NOW, client_factory=lambda *_: OfflineClient(routes()),
        extractor=extractor, relevance_fn=lambda jobs, prefs: jobs)
    path = tmp_path / "local" / "funded-watchlist.yaml"
    state = funding.State(path)
    assert not state.data["active"]
    assert any(d["reason"] == "daily_addition_deferred"
               for d in state.data["deals"].values())
    cfg.funding_daily_new_companies = 1
    funding.poll_once(
        cfg, now=NOW + timedelta(days=1),
        client_factory=lambda *_: OfflineClient(routes()),
        extractor=lambda *_a, **_kw: pytest.fail("persisted children were re-extracted"),
        relevance_fn=lambda jobs, prefs: jobs)
    state = funding.State(path)
    assert len(state.data["active"]) == 1
    cfg.funding_eligible_days = 30
    funding.poll_once(
        cfg, now=NOW + timedelta(days=93),
        client_factory=lambda *_: OfflineClient(routes(keep_opening=False)),
        extractor=lambda *_a, **_kw: pytest.fail("expired announcement was re-extracted"),
        relevance_fn=lambda jobs, prefs: jobs)
    first_absence = funding.State(path).data["active"]
    assert len(first_absence) == 1
    assert next(iter(first_absence.values()))["unproductive_since"]
    funding.poll_once(
        cfg, now=NOW + timedelta(days=184),
        client_factory=lambda *_: OfflineClient(routes(keep_opening=False)),
        extractor=lambda *_a, **_kw: pytest.fail("expired announcement was re-extracted"),
        relevance_fn=lambda jobs, prefs: jobs)
    assert funding.State(path).data["active"] == {}


def test_large_board_samples_without_trusting_later_duplicate_ids(tmp_path):
    cfg = settings(tmp_path)
    cfg.funding_max_postings_per_pass = 1
    fake = routes()
    data = json.loads(fixture("ashby.json"))
    original = data["jobs"][0]
    data["jobs"] = [original] + [
        {**original,
         "id": f"123e4567-e89b-42d3-a456-{i:012d}",
         "jobUrl": BOARD + f"/123e4567-e89b-42d3-a456-{i:012d}"}
        for i in range(1, 25)
    ]
    fake[ASHBY_API] = json.dumps(data)
    funding.poll_once(
        cfg, now=NOW, client_factory=lambda *_: OfflineClient(fake),
        extractor=extractor, relevance_fn=lambda jobs, prefs: jobs)
    assert len(funding.State(
        tmp_path / "local" / "funded-watchlist.yaml").data["active"]) == 1

    # The second occurrence lies outside the sample. It must still veto
    # the sampled posting identity rather than silently changing uniqueness.
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = settings(other_root)
    other.funding_max_postings_per_pass = 1
    original_copy = dict(original)
    duplicate = dict(original_copy)
    fake[ASHBY_API] = json.dumps({"jobs": [original_copy, duplicate]})
    funding.poll_once(
        other, now=NOW,
        client_factory=lambda *_: OfflineClient(fake),
        extractor=extractor, relevance_fn=lambda jobs, prefs: jobs)
    assert not funding.State(
        Path(other.local_dir) / "funded-watchlist.yaml").data["active"]


def test_source_limiter_rejects_out_of_contract_config(tmp_path):
    cfg = settings(tmp_path)
    cfg.funding_eligible_days = 365
    assert funding.poll_once(cfg) == {"status": "disabled"}
    cfg.funding_eligible_days = 30
    cfg.funding_max_postings_per_pass = 1000
    assert not funding.ready(cfg)


def test_report_tracks_deal_proofs_without_operator_or_person_data(tmp_path):
    cfg = settings(tmp_path)
    funding.poll_once(
        cfg, now=NOW, client_factory=lambda *_: OfflineClient(routes()),
        extractor=extractor, relevance_fn=lambda jobs, prefs: jobs)
    report = funding.State(
        tmp_path / "local" / "funded-watchlist.yaml").data["report"]
    counts = report["sources"]["uk_feed"]
    assert counts["fetched"] == 1
    assert counts["extracted_deal"] == 1
    assert counts["verified_homepage"] == 1
    assert counts["source_and_consumer_proved"] == 1
    assert counts["deal_active"] == 1
    assert counts["added"] == 1
    dumped = yaml.safe_dump(report).lower()
    assert "user-agent" not in dumped and "officer" not in dumped
    assert "secret" not in dumped


def test_sec_nonissuer_cannot_fallback_to_related_person_name(tmp_path):
    path = tmp_path / "funded-watchlist.yaml"
    state = funding.State(path)
    # A missing primaryIssuer is not cured by an entityName elsewhere.
    synthetic = b"""<?xml version="1.0"?><edgarSubmission>
    <relatedPersons><relatedPerson><entityName>Example Person</entityName></relatedPerson></relatedPersons>
    <offeringData><industryGroup><industryGroupType>Other Technology</industryGroupType></industryGroup>
    <offeringSalesAmounts><totalOfferingAmount>1000000</totalOfferingAmount></offeringSalesAmounts></offeringData>
    </edgarSubmission>"""
    client = OfflineClient({SEC_DOC: synthetic})
    funding._sec_proxy({"id": "sec_test"},
                       {"key": "nonissuer", "url": SEC_DOC},
                       state, client, NOW)
    deal = next(iter(funding.State(path).data["deals"].values()))
    assert deal["status"] == "rejected"
    assert deal["reason"] == "sec_issuer_or_sector_missing"
    assert deal["issuer"] == ""
    assert "Example Person" not in path.read_text()


def test_etag_crash_window_has_durable_unacknowledged_article(tmp_path):
    cfg = settings(tmp_path)
    path = tmp_path / "funded-watchlist.yaml"
    state = funding.State(path)
    source = funding._manifest(cfg)["sources"][0]
    client = OfflineClient({SOURCE: fixture("rss_uk.xml")})
    items, status = funding._source_payload(source, state, client, NOW)
    assert status == "fetched" and len(items) == 1
    # Simulate process death immediately after the source response. The cursor
    # MUST NOT exist without its corresponding unresolved article witness.
    restarted = funding.State(path)
    assert restarted.data["feeds"]["uk_feed"]["etag"] == '"synthetic-v1"'
    assert restarted.data["articles"][items[0]["key"]]["status"] == "queued"
    replay = OfflineClient({SOURCE: fixture("rss_uk.xml")})
    again, _ = funding._source_payload(source, restarted, replay, NOW)
    assert again == items
    assert replay.calls[0][1] == {}, "unacknowledged item bypasses conditional 304"
    restarted.data["articles"][items[0]["key"]]["status"] = "complete"
    restarted.save()
    conditional = OfflineClient({SOURCE: (304, b"")})
    data, status = funding._source_payload(source, restarted, conditional, NOW)
    assert data == [] and status == "not_modified"
    assert conditional.calls[0][1]["If-None-Match"] == '"synthetic-v1"'


def test_sec_transient_document_failure_retries_then_acknowledges(tmp_path):
    path = tmp_path / "funded-watchlist.yaml"
    state = funding.State(path)
    article = {"key": "synthetic-sec-retry", "url": SEC_INDEX}
    source = {"id": "sec_test"}
    unavailable = OfflineClient({})
    funding._sec_proxy(source, article, state, unavailable, NOW)
    deal = next(iter(state.data["deals"].values()))
    assert deal["status"] == "retryable"
    assert state.data["articles"][article["key"]]["status"] == "retryable"
    recovered = OfflineClient({
        SEC_INDEX: fixture("sec_index.html"),
        SEC_DOC: fixture("sec_primary_doc.xml"),
    })
    funding._sec_proxy(source, article, state, recovered, NOW)
    assert next(iter(state.data["deals"].values()))["status"] == "unresolved"
    assert state.data["articles"][article["key"]]["status"] == "complete"
    assert recovered.calls


def test_feed_rollover_keeps_unacknowledged_deal_link(tmp_path):
    cfg = settings(tmp_path)
    path = tmp_path / "funded-watchlist.yaml"
    state = funding.State(path)
    source = funding._manifest(cfg)["sources"][0]
    first, status = funding._source_payload(
        source, state, OfflineClient({SOURCE: fixture("rss_uk.xml")}), NOW)
    assert status == "fetched" and len(first) == 1
    old_key, old_url = first[0]["key"], first[0]["url"]
    # Publisher drops the old story from its 40 most recent items. The old
    # unacknowledged child still has a durable URL/date/title, not just a hash.
    latest = b"""<?xml version="1.0" encoding="utf-8"?><rss><channel>
    <item><guid>new-round</guid><title>OtherCo raises seed fintech funding in London</title>
    <link>https://news.example.com/new-round</link>
    <pubDate>Wed, 18 Nov 2026 12:00:00 GMT</pubDate></item>
    </channel></rss>"""
    restarted = funding.State(path)
    both, status = funding._source_payload(
        source, restarted, OfflineClient({SOURCE: latest}), NOW)
    assert status == "fetched" and len(both) == 2
    assert both[0]["key"] == old_key and both[0]["url"] == old_url
    persisted = funding.State(path).data["feeds"]["uk_feed"]["inflight"]
    assert {x["url"] for x in persisted} == {
        old_url, "https://news.example.com/new-round"}
    for row in both:
        restarted.data["articles"][row["key"]]["status"] = "complete"
    restarted.save()
    none, status = funding._source_payload(
        source, restarted, OfflineClient({SOURCE: (304, b"")}), NOW)
    assert none == [] and status == "not_modified"




@pytest.mark.parametrize("body,expected_reason", [
    (
        "example-co raised seed funding in Germany; "
        "OtherCo raised seed fintech financing in London.",
        "sector_unproved",
    ),
    (
        "example-co raised seed fintech funding in Germany while "
        "OtherCo raised seed fintech financing in London.",
        "region_unproved",
    ),
    (
        "example-co raised seed fintech funding in London; "
        "example-co raised seed fintech funding in Germany.",
        "region_unproved",
    ),
])
def test_two_issuer_passages_cannot_cross_authorize_sector_or_region(
        tmp_path, body, expected_reason):
    cfg = settings(tmp_path)
    news = funding._article(
        f'<h1>{body}</h1><a href="{HOME}">example-co</a>')
    deal = {
        "issuer": "example-co", "stage": "seed", "date": "2026-11-18",
        "sector": "fintech", "region": "uk", "remote_first": False,
    }
    reason, active, verified = funding._prove_deal(
        deal, {"source": "uk_feed", "date": "2026-11-18",
               "text": news.text, "html": news, "url": ARTICLE},
        funding.State(tmp_path / "local" / "funded-watchlist.yaml"),
        cfg, OfflineClient({}), NOW)
    assert (reason, active, verified) == (expected_reason, None, None)


def test_portfolio_page_with_valid_ashby_board_is_not_employer_homepage(tmp_path):
    cfg = settings(tmp_path)
    portfolio = "https://portfolio.example.org/companies/example-co"
    news = funding._article(
        '<h1>example-co raised seed fintech funding in London</h1>'
        f'<a href="{portfolio}">example-co</a>')
    outsider_page = (
        '<h1>Investments in technology</h1>'
        '<p>Portfolio company example-co is a fintech employer in London.</p>'
        f'<a href="{BOARD}">example-co jobs</a>'
    )
    client = OfflineClient({
        portfolio: outsider_page,
        BOARD: fixture("board_ashby.html"),
        ASHBY_API: fixture("ashby.json"),
    })
    deal = {"issuer": "example-co", "stage": "seed",
            "date": "2026-11-18", "sector": "fintech", "region": "uk"}
    reason, active, evidence = funding._prove_deal(
        deal, {"source": "uk_feed", "date": "2026-11-18",
               "text": news.text, "html": news, "url": ARTICLE},
        funding.State(tmp_path / "local" / "funded-watchlist.yaml"),
        cfg, client, NOW, relevance_fn=lambda jobs, prefs: jobs)
    assert (reason, active, evidence) == (
        "employer_homepage_owner_unproved", None, None)
    assert [url for url, *_ in client.calls] == [portfolio]
    assert BOARD in client.routes, "the tenant was valid, only homepage proof failed"


def test_saturated_source_replays_durable_backlog_across_restarts(tmp_path):
    cfg = settings(tmp_path)
    path = tmp_path / "funded-watchlist.yaml"
    state = funding.State(path)
    source = funding._manifest(cfg)["sources"][0]
    outstanding = []
    for i in range(500):
        key = f"{i:032x}"
        outstanding.append({
            "key": key,
            "title": "example-co raises seed fintech funding in London",
            "date": "2026-11-18",
            "url": f"https://news.example.com/older-{i}",
        })
        state.data["articles"][key] = {
            "status": "queued", "source": "uk_feed", "seen_at": NOW.isoformat(),
        }
    state.data["feeds"]["uk_feed"] = {
        "etag": '"previous"', "last_modified": "",
        "last_fetch": NOW.isoformat(), "inflight": outstanding,
    }
    state.save()
    at_capacity = OfflineClient({})
    replay, status = funding._source_payload(source, state, at_capacity, NOW)
    assert status == "backlog_deferred" and len(replay) == 500
    assert at_capacity.calls == [], "drain before fetching an uncheckpointable page"
    assert funding.State(path).data["feeds"]["uk_feed"]["etag"] == '"previous"'

    latest = b"""<?xml version="1.0"?><rss><channel>
    <item><guid>new-one</guid><title>OtherCo raises seed funding</title>
    <link>https://news.example.com/new-one</link>
    <pubDate>Wed, 18 Nov 2026 12:00:00 GMT</pubDate></item>
    <item><guid>new-two</guid><title>ThirdCo raises seed funding</title>
    <link>https://news.example.com/new-two</link>
    <pubDate>Wed, 18 Nov 2026 12:00:00 GMT</pubDate></item>
    </channel></rss>"""
    state.data["articles"][outstanding[0]["key"]]["status"] = "complete"
    state.save()
    restarted = funding.State(path)
    replay, status = funding._source_payload(
        source, restarted, OfflineClient({SOURCE: latest}), NOW)
    assert status == "backlog_deferred" and len(replay) == 499
    assert restarted.data["feeds"]["uk_feed"]["etag"] == '"previous"'
    assert len(restarted.data["articles"]) == 500
    restarted.data["articles"][outstanding[1]["key"]]["status"] = "complete"
    restarted.save()
    resumed = funding.State(path)
    replay, status = funding._source_payload(
        source, resumed, OfflineClient({SOURCE: latest}), NOW)
    assert status == "fetched" and len(replay) == 500
    assert resumed.data["feeds"]["uk_feed"]["etag"] == '"synthetic-v1"'
    assert {item["url"] for item in replay} >= {
        "https://news.example.com/new-one",
        "https://news.example.com/new-two",
        outstanding[-1]["url"],
    }
    assert len(funding.State(path).data["feeds"]["uk_feed"]["inflight"]) == 500


def test_productive_active_company_survives_90_day_relevance_recheck(tmp_path):
    cfg = settings(tmp_path)
    path = tmp_path / "local" / "funded-watchlist.yaml"
    funding.poll_once(
        cfg, now=NOW, client_factory=lambda *_: OfflineClient(routes()),
        extractor=extractor, relevance_fn=lambda jobs, prefs: jobs)
    original = funding.State(path).data["active"]
    assert len(original) == 1
    key = next(iter(original))
    future = NOW + timedelta(days=95)
    funding.poll_once(
        cfg, now=future, client_factory=lambda *_: OfflineClient(routes()),
        extractor=lambda *_a, **_kw: pytest.fail("old round was re-extracted"),
        relevance_fn=lambda jobs, prefs: jobs)
    refreshed = funding.State(path).data["active"]
    assert key in refreshed
    assert refreshed[key]["last_relevant_at"] == future.isoformat()
    assert refreshed[key]["last_checked_at"] == future.isoformat()
    assert "unproductive_since" not in refreshed[key]
    # A transient probe failure cannot be interpreted as proof of no jobs.
    after = future + timedelta(days=8)
    funding.poll_once(
        cfg, now=after,
        client_factory=lambda *_: OfflineClient({SOURCE: fixture("rss_uk.xml")}),
        extractor=lambda *_a, **_kw: pytest.fail("old round was re-extracted"),
        relevance_fn=lambda jobs, prefs: jobs)
    retained = funding.State(path).data["active"]
    assert key in retained
    assert retained[key]["last_relevant_at"] == future.isoformat()
    assert "unproductive_since" not in retained[key]

def test_sec_reader_refuses_missing_user_agent_before_any_request(tmp_path):
    cfg = settings(tmp_path)
    cfg.funding_sec_user_agent = ""
    state = funding.State(tmp_path / "funded-watchlist.yaml")
    source = {
        "id": "sec_test", "kind": "sec_atom", "region": "usa",
        "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=D&output=atom",
        "enabled": True,
    }
    client = OfflineClient({})
    with pytest.raises(funding.FundingRefusal, match="sec_user_agent_missing"):
        funding._source_payload(source, state, client, NOW, settings=cfg)
    assert client.calls == []
    assert state.data["feeds"] == {}
