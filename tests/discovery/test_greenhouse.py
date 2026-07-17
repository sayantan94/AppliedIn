import httpx
from discovery.adapters.greenhouse import GreenhouseAdapter
from discovery.watchlist import CompanyConfig

_FIXTURE = {
    "jobs": [
        {
            "id": 123,
            "title": "Senior Backend Engineer",
            "absolute_url": "https://acme.io/jobs/123",
            "content": "<p>Build &nbsp;distributed systems.</p>",
            "location": {"name": "Remote - US"},
        }
    ]
}


def test_greenhouse_parses_feed():
    def transport(request):
        assert "boards-api.greenhouse.io/v1/boards/acme/jobs" in str(request.url)
        return httpx.Response(200, json=_FIXTURE)

    client = httpx.Client(transport=httpx.MockTransport(transport))
    company = CompanyConfig(name="Acme", ats="greenhouse", board="acme")
    jobs = GreenhouseAdapter().fetch(company, client)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.pk == "acme#123"
    assert job.title == "Senior Backend Engineer"
    assert "distributed systems" in job.jd_text
    assert "<p>" not in job.jd_text  # html stripped
    assert job.location == "Remote - US"
