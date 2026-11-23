"""#404: offline source-bound posting employer and title evidence."""

from __future__ import annotations

import json

import pytest

from tools import jd


DETAIL = ("Build reliable integrations, implement independent source checks, "
          "and review synthetic posting workflows. ") * 8


def page(url, *, company="", title="Platform Engineer", hero=False,
         og_title="", og_company="", jsonld=True, graph=False, foreign_url="",
         unbound=False):
    fields = ""
    if jsonld:
        posting = {"@context": "https://schema.org", "@type": "JobPosting",
                   "title": title}
        if not unbound:
            posting["url"] = foreign_url or url
        if company:
            posting["hiringOrganization"] = {"@type": "Organization", "name": company}
        data = {"@graph": [{"@type": "Organization", "name": "Unrelated Inc"},
                           posting]} if graph else posting
        fields += ('<script type="application/ld+json">' + json.dumps(data) + '</script>')
    if og_title:
        fields += f'<meta property="og:title" content="{og_title}">'
    if og_company:
        fields += f'<meta property="og:site_name" content="{og_company}">'
    headings = ('<h1 class="hero">Build your future with us</h1>' if hero else '')
    headings += '<h2 class="job-title">Platform Engineer</h2>'
    return (f'<html><head><title>Generic careers landing</title>{fields}</head>'
            f'<body>{headings}<section><p>{DETAIL}</p></section></body></html>')


def read_page(monkeypatch, url, html, *, effective=None):
    monkeypatch.setattr(jd, "_from_new_ats", lambda *_: None)
    monkeypatch.setattr(jd, "_from_ats", lambda *_: None)
    seen = []
    def get(target, headers):
        seen.append(target)
        return html, effective or target
    monkeypatch.setattr(jd, "_get", get)
    monkeypatch.setattr(jd, "_from_chrome",
                        lambda *_: pytest.fail("synthetic HTML should bypass browser"))
    meta = jd.fetch_jd_meta(url)
    assert seen == [url]
    return meta


@pytest.mark.parametrize(("url", "company"), [
    ("https://jobs.example.com.vn/role/1", "Example Co"),
    ("https://example.breezy.hr/p/role", "Example Co"),
    ("https://example.homerun.co/role", "Example Co"),
    ("https://careers.example-careers.com/role/1", "Example Co"),
])
def test_source_jobposting_overrides_multisuffix_and_provider_hosts(
        monkeypatch, url, company):
    meta = read_page(monkeypatch, url, page(
        url, company=company, title="Senior Platform Engineer", graph=True,
        og_title="Build your future with us", hero=True))
    assert meta["company"] == company
    assert meta["title"] == "Senior Platform Engineer"
    assert meta["source_kind"] == "http_html"
    assert jd.pg.valid(meta["text"], meta["posting_guard"])
    assert jd.matched_source_witness({"jd_url": url}, meta)


def test_hero_and_generic_og_yield_to_actual_job_heading(monkeypatch):
    url = "https://careers.example-careers.com/role/1"
    meta = read_page(monkeypatch, url, page(
        url, jsonld=False, hero=True, og_title="Build your future with us"))
    assert meta["title"] == "Platform Engineer"
    assert not meta.get("company")


def test_job_specific_og_title_and_employer_are_source_evidence(monkeypatch):
    url = "https://careers.example-careers.com/role/1"
    meta = read_page(monkeypatch, url, page(
        url, jsonld=False, og_title="Staff Platform Engineer",
        og_company="Example Co", hero=True))
    assert meta["title"] == "Staff Platform Engineer"
    assert meta["company"] == "Example Co"


def test_unrelated_organization_is_not_a_hiring_employer(monkeypatch):
    url = "https://jobs.example.com.vn/role/1"
    meta = read_page(monkeypatch, url, page(
        url, title="Principal Platform Engineer", graph=True))
    assert meta["title"] == "Principal Platform Engineer"
    assert not meta.get("company")


@pytest.mark.parametrize("url", [
    "https://example.breezy.hr/p/role",
    "https://example.homerun.co/role",
])
def test_provider_site_without_employer_never_invents_organization(monkeypatch, url):
    meta = read_page(monkeypatch, url, page(url, jsonld=False))
    assert meta["title"] == "Platform Engineer"
    assert not meta.get("company")


def test_foreign_jobposting_and_foreign_redirect_cannot_prove_employer(monkeypatch):
    url = "https://jobs.example.com.vn/role/1"
    html = page(url, company="Foreign Example", title="Foreign Title",
                foreign_url="https://jobs.example.com.vn/role/2")
    meta = read_page(monkeypatch, url, html)
    assert not meta.get("company")
    assert meta["title"] == "Platform Engineer"
    redirected = read_page(monkeypatch, url, html,
                           effective="https://jobs.example.com.vn/role/2")
    assert not redirected.get("company")
    assert not jd.matched_source_witness({"jd_url": url}, redirected)


def test_workday_short_url_is_a_exact_native_destination():
    url = "https://example-co.wd3.myworkdayjobs.com/example/job/ROLE-1"
    parsed = jd._api_url(url)
    assert parsed is not None
    assert parsed[0] == "workday"
    assert parsed[2] == "/example/job/ROLE-1"
    assert parsed[3].endswith("/wday/cxs/example-co/example/job/ROLE-1")
    assert parsed[4]["tenant"] == "example-co"
    assert parsed[4]["site"] == "example"
    for bad in (
        "https://example-co.wd3.myworkdayjobs.com/example/job",
        "https://example-co.wd3.myworkdayjobs.com/example/job/../../ROLE-1",
        "https://other.wd3.myworkdayjobs.com/example/job/ROLE-1?x=1",
    ):
        assert jd._api_url(bad) is None


def test_short_workday_http_fallback_remains_guarded(monkeypatch):
    url = "https://example-co.wd3.myworkdayjobs.com/example/job/ROLE-1"
    meta = read_page(monkeypatch, url, page(url, company="Example Co", graph=True))
    assert meta["company"] == "Example Co"
    assert meta["title"] == "Platform Engineer"
    assert jd.matched_source_witness({"jd_url": url}, meta)


def test_navigation_heading_is_not_the_role_heading(monkeypatch):
    url = "https://careers.example-careers.com/role/1"
    html = ("<html><body><h1>About Us</h1><h2>Platform Engineer</h2><p>" +
            DETAIL + "</p></body></html>")
    meta = read_page(monkeypatch, url, html)
    assert meta["title"] == "Platform Engineer"


def test_provider_brand_in_same_page_jsonld_is_not_a_company(monkeypatch):
    url = "https://example.breezy.hr/p/role"
    meta = read_page(monkeypatch, url, page(
        url, company="Breezy HR", title="Platform Engineer"))
    assert not meta.get("company")
    assert meta["title"] == "Platform Engineer"


def test_unbound_unrelated_jobposting_cannot_override_unmarked_role_heading(monkeypatch):
    """source-01: uniqueness of URL-less JSON-LD does not bind a recommendation."""
    url = "https://jobs.example.com.vn/role/1"
    html = page(url, company="Different Fiction", title="Sales Manager",
                unbound=True).replace('class="job-title"', "")
    meta = read_page(monkeypatch, url, html)
    assert meta["title"] == "Platform Engineer"
    assert not meta.get("company")
    assert jd.matched_source_witness({"jd_url": url}, meta)


def test_unbound_related_role_cannot_override_marked_posting_heading(monkeypatch):
    """source-02: a related-but-different URL-less JSON-LD is not this posting."""
    url = "https://careers.example-careers.com/role/1"
    html = page(url, company="Different Fiction",
                title="Platform Engineering Manager", unbound=True, graph=True)
    meta = read_page(monkeypatch, url, html)
    assert meta["title"] == "Platform Engineer"
    assert not meta.get("company")
    assert jd.matched_source_witness({"jd_url": url}, meta)


def test_unbound_exact_role_can_supply_metadata(monkeypatch):
    """A corroborated URL-less JobPosting still supplies its employer and title."""
    url = "https://jobs.example.com.vn/role/1"
    meta = read_page(monkeypatch, url, page(
        url, company="Example Co", title="Platform Engineer", unbound=True))
    assert meta["title"] == "Platform Engineer"
    assert meta["company"] == "Example Co"


def test_unbound_seniority_mismatch_cannot_supply_employer(monkeypatch):
    """A recommendation for a related level is not independent role identity."""
    url = "https://jobs.example.com.vn/role/1"
    meta = read_page(monkeypatch, url, page(
        url, company="Different Fiction", title="Senior Platform Engineer",
        unbound=True))
    assert meta["title"] == "Platform Engineer"
    assert not meta.get("company")


def test_provider_og_site_brand_is_not_employer_identity(monkeypatch):
    """A job-specific OG title does not turn shared-provider site_name into employer."""
    url = "https://example.homerun.co/role"
    meta = read_page(monkeypatch, url, page(
        url, jsonld=False, og_title="Platform Engineer",
        og_company="Homerun Hiring Platform"))
    assert meta["title"] == "Platform Engineer"
    assert not meta.get("company")
    assert jd.matched_source_witness({"jd_url": url}, meta)


def test_unfamiliar_hero_h1_yields_to_plain_role_h2(monkeypatch):
    """A structural hero banner must not win on a newly invented slogan."""
    url = "https://careers.example-careers.com/role/1"
    html = page(url, jsonld=False).replace(
        '<h2 class="job-title">Platform Engineer</h2>',
        "<h1 class='hero'>Shape what's next</h1><h2>Platform Engineer</h2>")
    meta = read_page(monkeypatch, url, html)
    assert meta["title"] == "Platform Engineer"
    assert not meta.get("company")
