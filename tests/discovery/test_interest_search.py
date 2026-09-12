"""Broad discovery must produce grounded leads, not invented postings or JDs."""

from unittest.mock import Mock

import pytest

from discovery.interest_search import _posting_url, search
from discovery.interest_search import _verify_lead as _real_verify


@pytest.fixture(autouse=True)
def no_live_postings(monkeypatch):
    monkeypatch.setattr("discovery.interest_search._verify_lead", lambda row: row)


URL = "https://jobs.ashbyhq.com/new-company/real-posting"


def runner_for(jobs, source_urls=(URL,)):
    return Mock(
        return_value={
            "parsed": {"jobs": jobs, "summary": "Matches"},
            "source_urls": source_urls,
            "queries": ["software AI remote", "developer tools engineer"],
        }
    )


def job(url=URL, company="New Company"):
    return {
        "company": company,
        "url": url,
        "title": "Software Engineer",
        "location": "Remote",
        "why": "Matches infrastructure interests",
        "description": "Invented snippet must not become JD",
    }


def test_search_is_interest_first_and_keeps_only_tool_sourced_links():
    runner = runner_for([job(), job("https://jobs.ashbyhq.com/invented/made-up")])
    result = search(
        {"titles": ["Software Engineer"], "locations": ["Remote"]}, "developer tools", runner=runner
    )
    assert [j["url"] for j in result["jobs"]] == [URL]
    assert result["jobs"][0]["description"] == ""
    assert result["jobs"][0]["verification"] == "needs_posting_read"
    prompt = runner.call_args.args[0]
    assert "developer tools" in prompt and "company watchlist" in prompt


def test_company_search_cannot_return_another_employer_as_that_company():
    runner = runner_for([job(), job(URL + "/oracle", company="Oracle")], (URL, URL + "/oracle"))
    result = search({}, company="Oracle", runner=runner)
    assert [j["company"] for j in result["jobs"]] == ["Oracle"]
    assert "Only find roles at Oracle" in runner.call_args.args[0]


def test_incomplete_or_ungrounded_search_is_an_error_not_successful_zero_results():
    with pytest.raises(ValueError, match="verifiable posting"):
        search({}, runner=runner_for([job()], source_urls=[]))


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "http://jobs.example.com/role/1",
        "https://localhost/jobs/1",
        "https://10.0.0.1/jobs/1",
        "https://example.com/careers",
        "https://jobs.example.com/",
        "https://user:password@jobs.example.com/role/1",
    ],
)
def test_search_does_not_admit_unsafe_or_generic_career_pages(url):
    assert not _posting_url(url)


def test_direct_feed_verification_drops_closed_index_results_and_captures_real_jd():
    # Import the real verifier through the reference captured before the autouse stub.
    import httpx
    import respx

    from tools.jd import _text

    row = job("https://job-boards.greenhouse.io/acme/jobs/123")
    with respx.mock:
        route = respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs/123")
        route.mock(return_value=httpx.Response(404))
        assert _real_verify(row) is None
        content = "<p>Build developer tools and distributed systems. </p>" * 20
        route.mock(
            return_value=httpx.Response(200, json={"title": "Staff Engineer", "content": content})
        )
        verified = _real_verify(row)
        assert verified["description"] == _text(content)
        assert verified["title"] == "Staff Engineer" and verified["verification"] == "verified"
        route.mock(return_value=httpx.Response(503))
        assert _real_verify(row) == row, "A temporary feed outage is not proof the role is closed"


def test_checking_progress_reports_verified_results():
    messages = []
    result = search({}, runner=runner_for([job()]), on_progress=messages.append)
    assert any("Checking 1 posting" in message for message in messages)
    assert any("New Company" in message for message in messages)
    assert len(result["jobs"]) == 1


def test_interrupted_search_never_verifies_or_imports_partial_results(monkeypatch):
    verify = Mock()
    monkeypatch.setattr("discovery.interest_search._verify_lead", verify)
    with pytest.raises(ValueError, match="did not finish"):
        search({}, runner=Mock(side_effect=ValueError("Claude did not finish")))
    verify.assert_not_called()
