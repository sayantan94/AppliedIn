"""Many companies run a real ATS board behind a custom careers URL.

Databricks is the case that prompted this: its careers page is a JS app, so the
HTML never mentions Greenhouse and page-signature detection finds nothing. The
resolver fell back to ats=custom/CRAWL, the cheap render+extract returned 0
postings, and discovery escalated to a slow browser-use crawl — while a clean
Greenhouse API with 800 jobs sat one request away.

So before giving up and crawling, probe the known board APIs by company slug.
"""

from __future__ import annotations

import httpx
import pytest
from discovery.resolver import DiscoveryMode, probe_boards, resolve


def _client(handler) -> httpx.Client:  # noqa: ANN001
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_probe_finds_greenhouse_board_behind_a_custom_careers_page():
    def handler(request: httpx.Request) -> httpx.Response:
        if "boards-api.greenhouse.io/v1/boards/databricks/jobs" in str(request.url):
            return httpx.Response(200, json={"jobs": [{"id": 1, "title": "SWE"}]})
        return httpx.Response(404, json={})

    match = probe_boards("Databricks", "https://www.databricks.com/company/careers",
                         _client(handler))
    assert match is not None
    assert (match.ats, match.board) == ("greenhouse", "databricks")


def test_probe_uses_the_careers_domain_when_the_name_does_not_match():
    """'Google DeepMind' will never be a slug; the domain often is."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "job-boards.greenhouse.io" in str(request.url) or "lever" in str(request.url):
            return httpx.Response(404, json={})
        if "boards-api.greenhouse.io/v1/boards/acme/jobs" in str(request.url):
            return httpx.Response(200, json={"jobs": [{"id": 7}]})
        return httpx.Response(404, json={})

    match = probe_boards("Acme Research Labs", "https://acme.com/careers", _client(handler))
    assert match is not None and match.board == "acme"


def test_probe_ignores_an_empty_board():
    """A 200 with no jobs is not proof of a board — don't hijack discovery on it."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": []})

    assert probe_boards("Nobody", "https://nobody.example/careers", _client(handler)) is None


def test_probe_returns_none_when_nothing_answers():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    assert probe_boards("Nobody", "https://nobody.example/careers", _client(handler)) is None


def test_resolve_prefers_a_probed_board_over_falling_back_to_crawl():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://www.databricks.com"):
            return httpx.Response(200, text="<html><body>a JS app, no ATS in the HTML</body></html>")
        if "boards-api.greenhouse.io/v1/boards/databricks/jobs" in url:
            return httpx.Response(200, json={"jobs": [{"id": 1}]})
        return httpx.Response(404, json={})

    match = resolve("https://www.databricks.com/company/careers/open-positions",
                    _client(handler), name="Databricks")
    assert match.ats == "greenhouse"
    assert match.discovery is not DiscoveryMode.CRAWL


def test_resolve_still_crawls_when_there_is_genuinely_no_board():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "custom.example":
            return httpx.Response(200, text="<html>no ats here</html>")
        return httpx.Response(404, json={})

    match = resolve("https://custom.example/careers", _client(handler), name="Custom")
    assert match.ats == "custom"
    assert match.discovery is DiscoveryMode.CRAWL


@pytest.mark.parametrize("name,url,expected_slug", [
    ("Databricks", "https://www.databricks.com/careers", "databricks"),
    ("Scale AI", "https://scale.com/careers", "scaleai"),
])
def test_slug_candidates_cover_name_and_domain(name, url, expected_slug):  # noqa: ANN001
    from discovery.resolver import _slug_candidates

    assert expected_slug in _slug_candidates(name, url)


def test_registrable_domain_ignores_subdomains():
    """A multi-label careers host must reduce to the COMPANY label, not the first
    word — otherwise a marketing subdomain becomes the slug and collides with an
    unrelated board of that name."""
    from discovery.resolver import _registrable

    assert _registrable("explore.jobs.acme.net") == "acme"
    assert _registrable("www.acme.com") == "acme"
    assert _registrable("careers.example.co.uk") == "example"


def test_probe_rejects_a_board_belonging_to_another_company():
    """A slug collision must not pull a stranger's jobs into the pipeline."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "boards-api.greenhouse.io/v1/boards/explore/jobs" in url:
            return httpx.Response(200, json={"jobs": [{"id": 1}]})
        if url.rstrip("/").endswith("/boards/explore"):
            return httpx.Response(200, json={"name": "Explore"})   # a DIFFERENT company
        return httpx.Response(404, json={})

    assert probe_boards("Acme", "https://explore.jobs.acme.net/careers",
                        _client(handler)) is None


def test_probe_accepts_a_board_whose_name_matches_the_company():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "boards-api.greenhouse.io/v1/boards/databricks/jobs" in url:
            return httpx.Response(200, json={"jobs": [{"id": 1}]})
        if url.rstrip("/").endswith("/boards/databricks"):
            return httpx.Response(200, json={"name": "Databricks"})
        return httpx.Response(404, json={})

    m = probe_boards("Databricks", "https://www.databricks.com/careers", _client(handler))
    assert m is not None and m.board == "databricks"
