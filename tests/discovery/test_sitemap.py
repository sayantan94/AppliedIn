"""A careers site that refuses every plain request still publishes its sitemap.

Cloudflare turned away the page, the search and the JSON behind jobs.fidelity.com,
and a six-minute browser crawl came back with nothing. The sitemap answered at
once: 528 postings, each with its URL and the day it last changed. Search engines
need it, so it sits outside the bot wall."""

from __future__ import annotations

import httpx
import pytest

from discovery import crawler, sitemap
from discovery.watchlist import CompanyConfig, Preferences

INDEX = """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://jobs.acme.com/sitemap-pages.xml</loc></sitemap>
  <sitemap><loc>https://jobs.acme.com/sitemap-jobs.xml</loc></sitemap>
</sitemapindex>"""

PAGES = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://jobs.acme.com/en/</loc><lastmod>2026-08-21T09:28:47Z</lastmod></url>
  <url><loc>https://jobs.acme.com/en/jobs/</loc><lastmod>2026-07-08</lastmod></url>
  <url><loc>https://jobs.acme.com/en/teams/asset-management/</loc></url>
  <url><loc>https://jobs.acme.com/en/life-at-acme/benefits/</loc></url>
  <url><loc>https://jobs.acme.com/en/jobs/search-results/</loc></url>
</urlset>"""

JOBS = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://jobs.acme.com/en/jobs/2134422/senior-software-engineer-java/</loc>
       <lastmod>2026-09-04T00:00:00Z</lastmod></url>
  <url><loc>https://jobs.acme.com/en/jobs/2129664/senior-manager-tax/</loc>
       <lastmod>2026-07-22T00:00:00Z</lastmod></url>
  <url><loc>https://careers.acme.com/job/R-00817/staff-engineer-platform</loc></url>
  <url><loc>https://jobs.acme.com/en/job?jobId=9911&amp;lang=en</loc></url>
</urlset>"""


def _site(routes: dict):
    def handle(req: httpx.Request) -> httpx.Response:
        if str(req.url) in routes:
            body = routes[str(req.url)]
            return httpx.Response(200, content=body if isinstance(body, bytes) else body.encode())
        return httpx.Response(403 if req.url.path.startswith("/en") else 404, text="Just a moment...")
    return httpx.Client(transport=httpx.MockTransport(handle))


def test_follows_the_index_and_keeps_only_postings():
    client = _site({"https://jobs.acme.com/sitemap.xml": INDEX,
                    "https://jobs.acme.com/sitemap-pages.xml": PAGES,
                    "https://jobs.acme.com/sitemap-jobs.xml": JOBS})
    jobs = sitemap.sitemap_jobs("https://jobs.acme.com/en/", "Acme", client)
    by_id = {j.job_id: j for j in jobs}
    assert set(by_id) == {"2134422", "2129664", "R-00817"}, "an id with no slug has no title to screen"
    j = by_id["2134422"]
    assert j.title == "Senior Software Engineer Java"
    assert j.posted_at == "2026-09-04T00:00:00Z"
    assert j.company == "Acme"
    assert j.jd_url == "https://jobs.acme.com/en/jobs/2134422/senior-software-engineer-java/"
    assert by_id["R-00817"].title == "Staff Engineer Platform"
    assert by_id["R-00817"].posted_at == ""


def test_reads_the_sitemap_named_in_robots_when_the_default_is_missing():
    client = _site({"https://jobs.acme.com/robots.txt": "User-agent: *\nSitemap: https://jobs.acme.com/xml/jobs.xml\n",
                    "https://jobs.acme.com/xml/jobs.xml": JOBS})
    jobs = sitemap.sitemap_jobs("https://jobs.acme.com/en/search-results?keywords=x", "Acme", client)
    assert len(jobs) == 3


def test_a_gzipped_sitemap_is_read():
    import gzip
    client = _site({"https://jobs.acme.com/sitemap.xml": gzip.compress(JOBS.encode())})
    assert len(sitemap.sitemap_jobs("https://jobs.acme.com/", "Acme", client)) == 3


def test_nothing_when_there_is_no_sitemap():
    assert sitemap.sitemap_jobs("https://jobs.acme.com/en/", "Acme", _site({})) == []


@pytest.mark.parametrize("url,expected", [
    ("https://jobs.acme.com/en/jobs/", False),
    ("https://jobs.acme.com/en/jobs/search-results/", False),
    ("https://jobs.acme.com/en/job-categories/", False),
    ("https://jobs.acme.com/en/jobs/2134422/senior-software-engineer-java/", True),
    ("https://acme.com/careers/openings/software-engineer-backend-remote", True),
    ("https://acme.com/careers/benefits", False),
    ("https://acme.com/careers/job/12345", True),
    ("https://acme.com/about/press/2026-09-04-launch", False),
])
def test_what_counts_as_a_posting(url, expected):
    assert sitemap.is_job_url(url) is expected


class _Tracking:
    def __init__(self):
        self.put = []
        self.r = None

    def put_new(self, job):
        self.put.append(job)
        return True


class _Queue:
    def enqueue(self, *_a, **_k):
        pass


class _Stores:
    tracking = None

    def __init__(self):
        self.tracking = _Tracking()
        self.queue = _Queue()
        self.tailor_queue = "tailor"


def test_crawl_screens_the_sitemap_instead_of_opening_the_browser(monkeypatch):
    from core import flags
    from tools import seen

    monkeypatch.setattr(crawler, "_render_page", lambda url: None)
    monkeypatch.setattr(flags, "effective_prefs", lambda name, prefs: prefs)
    monkeypatch.setattr(flags, "company_pref", lambda name: {})
    monkeypatch.setattr(flags, "company_filter", lambda name: [])
    monkeypatch.setattr(seen, "load", lambda: set())
    monkeypatch.setattr(seen, "mark", lambda jobs: None)
    monkeypatch.setattr(crawler, "_age_limit", lambda name: 0.0)
    monkeypatch.setattr(crawler, "relevant", lambda jobs, prefs: [j for j in jobs if "Engineer" in j.title])
    monkeypatch.setattr(crawler, "_browser_extract",
                        lambda *a, **k: pytest.fail("the browser must not open when the sitemap answered"))
    client = _site({"https://jobs.acme.com/sitemap.xml": JOBS})
    monkeypatch.setattr(crawler, "sitemap_jobs",
                        lambda url, name, c=None: sitemap.sitemap_jobs(url, name, client))
    noted = []
    monkeypatch.setattr(flags, "note_fetch_refused", lambda name: noted.append(name))
    stores = _Stores()
    co = CompanyConfig(name="Acme", careers_url="https://jobs.acme.com/en/")
    n = crawler.crawl_company(co, Preferences(), stores, client=client)
    assert n == 2
    assert {j.title for j in stores.tracking.put} == {"Senior Software Engineer Java", "Staff Engineer Platform"}
    assert noted == ["Acme"]


def test_browser_mode_cannot_be_short_circuited_by_a_sitemap(monkeypatch):
    """Starbucks returned a small irrelevant sitemap slice and never searched.

    Even a refused HTTP fetch used to bypass the browser-mode override. Neither
    HTTP nor a sitemap may decide that an explicitly requested search is done.
    """
    from core import flags
    from core.models import DiscoveryMode, JobRecord

    def cheap_reader(*args, **kwargs):
        pytest.fail("browser mode must search even when a sitemap has entries")

    monkeypatch.setattr(crawler, "_render_page", cheap_reader)
    monkeypatch.setattr(crawler, "sitemap_jobs", cheap_reader)
    monkeypatch.setattr(crawler, "_default_extractor", cheap_reader)
    monkeypatch.setattr(flags, "effective_prefs", lambda name, prefs: prefs)
    monkeypatch.setattr(flags, "company_pref", lambda name: {})
    monkeypatch.setattr(flags, "company_filter", lambda name: ["Principal"])
    monkeypatch.setattr(crawler, "relevant", lambda jobs, prefs: jobs)
    monkeypatch.setattr(crawler, "_age_limit", lambda name: 0.0)
    calls = []
    found = [JobRecord(company="starbucks", job_id=str(i), title=title, jd_text="",
                       jd_url=f"https://starbucks.eightfold.ai/careers/job/{i}")
             for i, title in enumerate(["Principal Engineer AI", "Software Engineer"])]

    def browser(url, company, prefs):
        calls.append((url, company))
        return found

    monkeypatch.setattr(crawler, "_browser_extract", browser)
    co = CompanyConfig(name="starbucks", careers_url="https://starbucks.eightfold.ai/careers",
                       discovery=DiscoveryMode.BROWSER)
    stores = _Stores()
    client = httpx.Client(transport=httpx.MockTransport(cheap_reader))
    assert crawler.crawl_company(co, Preferences(), stores, client=client) == 1
    assert calls == [(co.careers_url, co.name)]
    assert [j.title for j in stores.tracking.put] == ["Principal Engineer AI"]


@pytest.mark.parametrize("url,location", [
    ("https://jobs.acme.com/ie/jobs/2131328/software-engineer/", "Ireland"),
    ("https://jobs.acme.com/in/jobs/2131328/software-engineer/", "India"),
    ("https://jobs.acme.com/en-gb/jobs/2131328/software-engineer/", "United Kingdom"),
    ("https://jobs.acme.com/en/jobs/2131328/software-engineer/", ""),
    ("https://jobs.acme.com/en/jobs/2131328/software-engineer-dublin/", ""),
])
def test_the_country_in_the_url_becomes_a_location_hint(url, location):
    job = sitemap.job_from_url("Acme", url)
    assert job is not None
    assert job.location == location
