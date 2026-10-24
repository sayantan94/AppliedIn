"""a16z portfolio search: the server action, its id, and a staging path that never applies."""
# ruff: noqa: F811  (the shared `board` fixture is imported, then requested by name)

import json

import httpx
import pytest

from discovery import a16z, cosign
from tests.discovery.test_cosign import board  # noqa: F401

ACTION_ID = "607219795472ea0cd8e0bb777617d6bf20547cefc3"


def role(rid, title="Senior Software Engineer", **extra):
    return {
        "id": f"ats:{rid}",
        "ats_posting_id": rid,
        "title": title,
        "apply_url": f"https://jobs.ashbyhq.com/acme/{rid}/application",
        "location": "Seattle · Seattle, Washington, United States",
        "remote": False,
        "team": "Platform",
        "employment_type": "FullTime",
        "salary_currency": "USD",
        "salary_min": 200000,
        "salary_max": 260000,
        "salary_period": "year",
        "posted_at": "2026-10-23T18:55:14.565Z",
        "active": True,
        "company_name": "Acme",
        "company_logo": "https://img.example/acme.svg",
        **extra,
    }


def rsc(jobs, total=None, done=True):
    page = {"jobs": jobs, "total": total if total is not None else len(jobs), "page": 0,
            "perPage": 50, "isDone": done}
    return '0:{"a":"$@1","f":"","b":"x"}\n1:' + json.dumps(page) + "\n"


def site(pages, calls=None, *, action=ACTION_ID):
    """jobs.a16z.com: a jobs page, one script naming the action, and the action,
    which answers each call with the next canned page (or a status code)."""
    replies = iter(pages)

    def handle(request):
        if request.method == "GET" and request.url.path == "/jobs":
            return httpx.Response(200, text='<script src="/_next/static/chunks/a.js"></script>')
        if request.method == "GET":
            return httpx.Response(200, text=(
                'let d=(0,c.createServerReference)("' + action
                + '",c.callServer,void 0,c.findSourceMapURL,"loadMorePublicJobs")'))
        if calls is not None:
            calls.append({"action": request.headers.get("next-action"),
                          "args": json.loads(request.content)})
        reply = next(replies)
        if isinstance(reply, int):
            return httpx.Response(reply, text="Server action not found.")
        return httpx.Response(200, text=reply)

    return httpx.Client(transport=httpx.MockTransport(handle))


@pytest.fixture(autouse=True)
def fresh_action_id():
    a16z._ACTION_ID["value"] = None
    yield
    a16z._ACTION_ID["value"] = None


def test_the_action_id_is_read_from_the_sites_own_scripts(board):
    calls = []
    filters = {"term": "software engineer", "region": "us"}
    a16z.search(filters, client=site([rsc([role("a")])], calls))
    assert calls[0]["action"] == ACTION_ID
    filters, paging = calls[0]["args"]
    assert filters == {"sort": "recent", "q": "software engineer", "locations": ["country:us"]}
    assert paging == {"page": 0, "limit": a16z.PAGE}


def test_a_retired_action_id_is_read_again_once(board):
    a16z._ACTION_ID["value"] = "stale"
    calls = []
    out = a16z.search({}, client=site([404, rsc([role("a")])], calls))
    assert [c["action"] for c in calls] == ["stale", ACTION_ID]
    assert len(out["jobs"]) == 1


def test_a_board_that_keeps_refusing_is_an_error_not_an_empty_page(board):
    with pytest.raises(a16z.A16zError):
        a16z.search({}, client=site([404, 404]))


def test_a_place_replaces_the_us_filter():
    assert a16z._args({"region": "us", "city": "metro:greater-seattle-area"})["locations"] == [
        "metro:greater-seattle-area"]
    assert "locations" not in a16z._args({"region": ""})


@pytest.mark.parametrize(
    "bad", [{"role": "x-bat-division"}, {"city": "city:5391959"}, {"region": "eu"}]
)
def test_unknown_filters_are_refused_before_reaching_the_board(bad):
    with pytest.raises(ValueError):
        a16z._args(bad)


def test_results_link_the_posting_not_its_apply_step(board):
    out = a16z.search({}, client=site([rsc([
        role("a"),
        role("b", apply_url="https://jobs.lever.co/acme/b/apply"),
        role("c", apply_url="https://boards.greenhouse.io/acme/jobs/123?gh_jid=123"),
    ])]))
    assert [j["url"] for j in out["jobs"]] == [
        "https://jobs.ashbyhq.com/acme/a",
        "https://jobs.lever.co/acme/b",
        "https://job-boards.greenhouse.io/acme/jobs/123?gh_jid=123",
    ]


def test_search_drops_closed_roles_flags_exclusions_and_pages_by_number(board):
    out = a16z.search({}, client=site([rsc([
        role("a"), role("b", title="Software Engineer Intern"), role("c", active=False),
    ], total=120, done=False)]))
    assert [j["id"] for j in out["jobs"]] == ["a16z:ats:a", "a16z:ats:b"]
    assert out["jobs"][1]["excluded"] == "intern"
    assert out["cursor"] == "1" and not out["done"]
    assert out["jobs"][0]["posted"] == 1792781714565


def test_only_yearly_usd_pay_is_shown_as_salary(board):
    out = a16z.search({}, client=site([rsc([role("a", salary_currency="EUR"),
                                            role("b", salary_period="hour")])]))
    assert all(j["salary_min"] is None for j in out["jobs"])


def test_staging_prepares_only_and_records_the_real_ats(board):
    a16z.search({}, client=site([rsc([role("a")])]))
    result = a16z.stage(["a16z:ats:a"])
    assert result["prepared"] == 1
    row = board.tracking.get(result["pks"][0])
    assert row["discovery_source"] == "career_ops"
    assert row["apply_requested_at"] == ""
    assert row["ats"] == "ashby"
    assert row["jd_url"] == "https://jobs.ashbyhq.com/acme/a"
    # Staging twice never creates a second application.
    assert a16z.stage(["a16z:ats:a"])["prepared"] == 0


def test_a_posting_id_outside_the_url_still_names_the_job(board):
    # An ADP page's last segment is "recruitment.html" for every role there.
    url = "https://workforcenow.adp.com/x/recruitment.html?cid=1&jobId=592680"
    a16z.search({}, client=site([rsc([role("592680", apply_url=url)])]))
    result = a16z.stage(["a16z:ats:592680"])
    assert board.tracking.get(result["pks"][0])["job_id"] == "592680"


def test_only_explicit_apply_marks_a_role_for_submission(board):
    a16z.search({}, client=site([rsc([role("a")])]))
    result = a16z.stage(["a16z:ats:a"], apply_requested=True)
    assert board.tracking.get(result["pks"][0])["apply_requested_at"]


def test_staging_refuses_ids_that_are_not_a16z_results(board):
    cosign._remember([{"id": "forged"}])
    with pytest.raises(ValueError):
        a16z.stage(["forged"])
    with pytest.raises(ValueError):
        a16z.stage(["a16z:never-fetched"])


@pytest.mark.parametrize("place,value", [
    ("Bellevue, WA", "metro:greater-seattle-area"),
    ("San Francisco", "metro:san-francisco-bay-area"),
    ("NYC", "metro:new-york-city-metropolitan-area"),
    ("London, UK", "country:gb"),
    ("Washington, DC", ""),
    ("Remote", ""),
])
def test_preference_locations_map_to_the_boards_places(place, value):
    assert a16z.place_for(place) == value
