"""A filtered or duplicate result is not evidence of an unreadable page."""
from types import SimpleNamespace

import fakeredis

from discovery import handler, progress, scan_log
from discovery.watchlist import CompanyConfig, Preferences


def test_scan_receipt_keeps_read_matched_and_new_counts_distinct(monkeypatch):
    from core.models import DiscoveryMode
    from discovery import crawler
    r = fakeredis.FakeRedis(decode_responses=True)
    stores = SimpleNamespace(tracking=SimpleNamespace(r=r))
    companies = [CompanyConfig(name="Acme", careers_url="https://example.com",
                               discovery=DiscoveryMode.BROWSER),
                 CompanyConfig(name="Beta", careers_url="https://example.org",
                               discovery=DiscoveryMode.BROWSER)]
    monkeypatch.setattr(handler, "make_stores", lambda *a: stores)
    monkeypatch.setattr(handler, "get_settings", lambda: SimpleNamespace(config_dir="unused"))
    monkeypatch.setattr(handler, "load_preferences", lambda *a: Preferences())
    monkeypatch.setattr(handler, "load_watchlist", lambda *a: companies)
    monkeypatch.setattr(handler, "_stop_requested", lambda *a: False)
    calls = []

    def resolve(company, client):
        calls.append("resolve " + company.name)
        return company

    def crawl(company, *a):
        calls.append("read " + company.name)
        progress.record(found=196, relevant=12)
        return 0

    monkeypatch.setattr(handler, "resolve_company", resolve)
    monkeypatch.setattr(crawler, "crawl_company", crawl)
    result = handler._run_discovery(["Acme", "Beta"], "", True, 0)
    assert calls == ["resolve Acme", "read Acme", "resolve Beta", "read Beta"], (
        "the first scan must not wait for every other company's resolution")
    assert result["companies"][0]["found"] == 196
    assert result["companies"][0]["relevant"] == 12
    assert result["companies"][0]["enqueued"] == 0
    assert scan_log.results(r)["companies"][0]["found"] == 196


def test_stopping_one_scan_does_not_stop_another(monkeypatch):
    acme, beta = progress.Scan("Acme"), progress.Scan("Beta")
    monkeypatch.setattr(handler, "_SCANS", {"acme": acme, "beta": beta})
    assert handler.stop_company("ACME")
    assert acme.stopped.is_set() and not beta.stopped.is_set()
    token = progress.current.set(acme)
    try:
        assert progress.cancelled()
    finally:
        progress.current.reset(token)
    assert not progress.cancelled()
    assert not handler.stop_company("Missing")


def test_missing_counts_stay_unknown_instead_of_claiming_nothing_was_read():
    r = fakeredis.FakeRedis(decode_responses=True)
    scan_log.finished(r, "Acme", found=None, relevant=None, enqueued=0, seconds=1,
                      note="Could not connect")
    assert scan_log.results(r)["companies"][0]["found"] is None


def test_numeric_prefixed_title_slugs_are_not_discarded_as_ids():
    from discovery.sitemap import job_from_url
    job = job_from_url("Starbucks", "https://example.com/careers/job/12345678-principal-software-engineer")
    assert job is not None and job.title == "Principal Software Engineer"
