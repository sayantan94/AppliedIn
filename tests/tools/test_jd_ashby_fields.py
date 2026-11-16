"""Offline Ashby ATS and final-reader reconciliation with source identity intact."""

from types import SimpleNamespace

import pytest

from discovery.adapters.ashby import AshbyAdapter
from discovery.relevance import classify_location_fit
from discovery.watchlist import CompanyConfig
from tools import jd as J
from tools import posting_guard as pg


URL = "https://jobs.ashbyhq.com/example-co/synthetic-1"
DIFFERENT = "https://jobs.ashbyhq.com/example-co/other-job"
LONG = "Remote — APAC. " + "Build reliable synthetic integration services. " * 20


def posting(body=LONG, **fields):
    return {"id": "synthetic-1", "jobUrl": URL,
            "title": "Synthetic Platform Engineer",
            "descriptionPlain": body,
            "descriptionHtml": "<p>" + body + "</p>",
            **fields}


class FakeHTTP:
    def __init__(self, jobs):
        self.jobs = jobs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def get(self, *args, **kwargs):
        return SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"jobs": self.jobs})


def setup(monkeypatch, j):
    monkeypatch.setattr(J.httpx, "Client", lambda **kwargs: FakeHTTP([j]))
    company = CompanyConfig(name="example-co", board="example-co", ats="ashby")
    feed = AshbyAdapter().fetch(company, FakeHTTP([j]))[0]
    assert pg.valid(feed.jd_text, feed.posting_guard)
    return feed


def assert_source(result):
    assert pg.valid(result["text"], result["posting_guard"])
    assert result["source_url"] == URL
    assert result["source_kind"] == "ats_html"
    assert "_ashby_metadata" not in result
    assert "_ashby_body_length" not in result


@pytest.mark.parametrize("fields,expected", [
    ({"location": {"name": "Japan"}, "secondaryLocations": [{"name": "US"}],
      "workplaceType": "Remote", "isRemote": True}, "in_class"),
    ({"location": "Japan", "secondaryLocations": [], "isRemote": False,
      "workplaceType": "Remote"}, "unknown"),
    ({}, "in_class"),
])
def test_long_ats_and_feed_same_version(monkeypatch, fields, expected):
    j = posting(**fields)
    feed = setup(monkeypatch, j)
    monkeypatch.setattr(J, "_get", lambda *a, **k: pytest.fail("long ATS must win"))
    fetched = J.fetch_jd_meta(URL)
    assert_source(fetched)
    assert classify_location_fit(feed.jd_text) == expected
    assert classify_location_fit(fetched["text"]) == expected
    for key in fields:
        assert f"Ashby {key}:" in fetched["text"]


@pytest.mark.parametrize("fields,expected", [
    ({"location": "Japan", "secondaryLocations": [], "workplaceType": "Remote",
      "isRemote": True}, "in_class"),
    ({"workplaceType": "Remote", "isRemote": False}, "unknown"),
    ({}, "in_class"),
])
def test_short_ats_falls_back_to_same_job_html(monkeypatch, fields, expected):
    # The metadata can exceed 200 bytes, but the ATS *description* remains short.
    short = "Remote — APAC"
    j = posting(short, **fields)
    feed = setup(monkeypatch, j)
    page = "<html><title>Synthetic Platform Engineer</title><body><h1>Role</h1>" + (
        "<p>Remote — APAC. Deliver synthetic integration services.</p>" * 15) + "</body></html>"
    monkeypatch.setattr(J, "_get", lambda *a, **k: (page, URL))
    monkeypatch.setattr(J, "_from_chrome",
                        lambda *a, **k: pytest.fail("long HTML should win"))
    selected = J.fetch_jd_meta(URL)
    assert_source(selected)
    assert "Deliver synthetic integration services" in selected["text"]
    assert classify_location_fit(feed.jd_text) == expected
    assert classify_location_fit(selected["text"]) == expected
    for key in fields:
        assert f"Ashby {key}:" in selected["text"]


def test_short_ats_browser_fallback_keeps_proven_metadata(monkeypatch):
    j = posting("Remote — APAC", location={"name": "Japan"},
                secondaryLocations=[], workplaceType="Remote", isRemote=True)
    feed = setup(monkeypatch, j)
    monkeypatch.setattr(J, "_get", lambda *a, **k: None)
    monkeypatch.setattr(J, "_from_chrome",
                        lambda *a, **k: pg.analyze("Remote — APAC. " + LONG))
    result = J.fetch_jd_meta(URL)
    assert pg.valid(result["text"], result["posting_guard"])
    assert "Ashby isRemote: true" in result["text"]
    assert "Ashby secondaryLocations: []" in result["text"]
    assert classify_location_fit(result["text"]) == classify_location_fit(feed.jd_text)
    assert result.get("source_identity") is None


def test_same_host_different_job_redirect_does_not_replace_original(monkeypatch):
    j = posting("Remote — Japan-only", workplaceType="Remote", isRemote=True)
    setup(monkeypatch, j)
    html = "<html><body>" + ("Remote — APAC. Unrelated posting. " * 25) + "</body></html>"
    monkeypatch.setattr(J, "_get", lambda *a, **k: (html, DIFFERENT))
    monkeypatch.setattr(J, "_from_chrome", lambda *a, **k: None)
    result = J.fetch_jd_meta(URL)
    assert_source(result)
    assert classify_location_fit(result["text"]) == "out_of_class"
    assert "Unrelated posting" not in result["text"]
    assert result.get("source_identity") == {}


def test_unmatched_ats_job_cannot_supply_metadata(monkeypatch):
    j = {**posting("Remote — Japan-only", workplaceType="Remote"), "id": "wrong",
         "jobUrl": DIFFERENT}
    setup(monkeypatch, j)
    monkeypatch.setattr(J, "_get", lambda *a, **k: None)
    monkeypatch.setattr(J, "_from_chrome", lambda *a, **k: None)
    result = J.fetch_jd_meta(URL)
    assert pg.valid(result["text"], result["posting_guard"])
    assert "Ashby" not in result["text"]
    assert result["text"] == ""


def test_different_versions_are_not_forced_to_agree(monkeypatch):
    old = posting("Remote worldwide", workplaceType="Remote")
    feed = setup(monkeypatch, old)
    newer = posting("Remote US-only", workplaceType="Remote")
    # A current reread may legitimately differ from an old feed snapshot.
    monkeypatch.setattr(J.httpx, "Client", lambda **kwargs: FakeHTTP([newer]))
    monkeypatch.setattr(J, "_get", lambda *a, **k: None)
    monkeypatch.setattr(J, "_from_chrome", lambda *a, **k: None)
    result = J.fetch_jd_meta(URL)
    assert classify_location_fit(feed.jd_text) == "in_class"
    assert classify_location_fit(result["text"]) == "out_of_class"
    assert pg.valid(result["text"], result["posting_guard"])


def test_long_feed_only_guard_forces_existing_matched_source_reread(monkeypatch):
    # A long, otherwise clean Ashby feed is NOT an inspected HTML source.
    import asyncio

    from agent import run as R

    j = posting("Remote — APAC. " + "Build synthetic services. " * 50,
                location="Vietnam", workplaceType="Remote", isRemote=True)
    feed = setup(monkeypatch, j)
    assert len(feed.jd_text) >= 400
    assert feed.posting_guard["status"] == "unchecked"
    assert feed.posting_guard["source_verified"] is False
    assert "source_html_unavailable" in feed.posting_guard["reasons"]

    read_calls = []
    actual_reader = J.fetch_jd_meta

    def observed_reader(url, purpose):
        read_calls.append((url, purpose))
        return actual_reader(url, purpose)

    monkeypatch.setattr(J, "fetch_jd_meta", observed_reader)
    monkeypatch.setattr(J, "_get", lambda *a, **k:
                        pytest.fail("long matched ATS HTML should win"))
    row = {"jd_url": URL, "jd_text": feed.jd_text,
           "posting_guard": feed.posting_guard, "job_id": feed.job_id,
           "ats": "ashby", "company": "example-co"}
    selected = asyncio.run(R._jd_meta(row))
    assert read_calls == [(URL, "jd_sweep")]
    assert selected["source_kind"] == "ats_html"
    assert selected["source_url"] == URL
    assert selected["posting_guard"]["source_verified"] is True
    assert "source_html_unavailable" not in selected["posting_guard"]["reasons"]
    assert pg.valid(selected["text"], selected["posting_guard"])
    assert classify_location_fit(selected["text"]) == classify_location_fit(feed.jd_text)


def test_metadata_survives_guard_truncation_in_ats(monkeypatch):
    j = posting("Remote — APAC. " + "service " * 20000,
                location="Vietnam", workplaceType="Remote", isRemote=True)
    setup(monkeypatch, j)
    result = J._from_ats(URL)
    assert len(result["text"]) == 100000
    assert result["text"].startswith('Ashby location: "Vietnam"')
    assert pg.valid(result["text"], result["posting_guard"])
