import httpx
from appliedin_core.models import DiscoveryMode
from appliedin_discovery.resolver import detect_from_url, resolve


def test_greenhouse_board_url():
    m = detect_from_url("https://boards.greenhouse.io/acmecorp")
    assert (m.ats, m.board) == ("greenhouse", "acmecorp")


def test_greenhouse_embed_for_param():
    m = detect_from_url("https://boards.greenhouse.io/embed/job_board?for=acmecorp")
    assert (m.ats, m.board) == ("greenhouse", "acmecorp")


def test_lever_and_ashby_and_smartrecruiters():
    assert detect_from_url("https://jobs.lever.co/leverco").board == "leverco"
    assert detect_from_url("https://jobs.ashbyhq.com/ashco").ats == "ashby"
    assert detect_from_url("https://careers.smartrecruiters.com/SmartCo").board == "SmartCo"


def test_workday_builds_cxs_base():
    m = detect_from_url("https://acme.wd1.myworkdayjobs.com/en-US/AcmeCareers")
    assert m.ats == "workday"
    assert m.board == "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/AcmeCareers/jobs"


def test_custom_url_falls_back_to_crawl():
    m = detect_from_url("https://www.acme.com/careers")
    assert m is None  # no direct pattern


def test_resolve_scans_page_for_embedded_ats():
    html = '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acmecorp"></iframe>'
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    m = resolve("https://www.acme.com/careers", client)
    assert (m.ats, m.board) == ("greenhouse", "acmecorp")


def test_resolve_unrecognized_page_is_crawl():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>no ats here</html>")))
    m = resolve("https://www.acme.com/careers", client)
    assert m.ats == "custom"
    assert m.discovery is DiscoveryMode.CRAWL
