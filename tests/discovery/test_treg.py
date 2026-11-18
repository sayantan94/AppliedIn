"""Treg cross-company search reads one explicit page at a time."""

import json

import httpx
import pytest

from discovery import treg, treg_api
from tests.discovery.test_cosign import board  # noqa: F401

URL = "https://job-boards.greenhouse.io/example/jobs/123"


@pytest.fixture(autouse=True)
def key_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(treg, "ENV_FILE", path)
    monkeypatch.setattr(treg.config, "TREG_ENV_TOKEN", None)
    return path


def test_setup_and_search_read_the_current_key(key_file):
    assert treg_api.options()["configured"] is False
    with pytest.raises(treg.TregError, match="TREG_TOKEN"):
        treg.search({})
    key_file.write_text("TREG_TOKEN=first-team-key\n")
    assert treg_api.options()["configured"] is True
    assert treg.team_key() == "first-team-key"
    key_file.write_text("TREG_TOKEN=new-team-key\n")
    assert treg.team_key() == "new-team-key"
    key_file.unlink()
    assert treg_api.options()["configured"] is False


def test_exported_key_keeps_precedence(key_file, monkeypatch):
    monkeypatch.setattr(treg.config, "TREG_ENV_TOKEN", "exported-token")
    key_file.write_text("TREG_TOKEN=file-token\n")
    assert treg.team_key() == "exported-token"


def test_paid_page_limits_and_deduplicates_employer_urls(board, key_file):  # noqa: F811
    key_file.write_text("TREG_TOKEN=mock-token\n")
    job = {
        "application_link": URL,
        "job_title": "Engineer",
        "company_name": "Example",
        "location_raw": "Seattle",
        "salary_range": {"min": 100000, "currency": "USD", "period": "yearly"},
    }

    def reply(request):
        assert str(request.url) == treg.ENDPOINT
        assert request.headers["X-Treg-Token"] == "mock-token"
        body = json.loads(request.content)
        assert (body["page"], body["limit"], body["job_titles"]) == (2, 10, ["Engineer"])
        assert body["geo_locations"] == [{"city": "Seattle", "country": "United States"}]
        return httpx.Response(
            200, json={"jobs": [job, {**job, "location_raw": "Bellevue"}], "total_pages": 3}
        )

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        page = treg.search({"term": "Engineer", "city": "seattle"}, "2", client)
    assert page["cursor"] == "3" and not page["done"]
    (row,) = page["jobs"]
    assert row["salary_min"] == 100000 and row["location"] == "Seattle · Bellevue"
    assert not board.tracking.all()

    (pk,) = treg.stage([row["id"]])["pks"]
    record = board.tracking.get(pk)
    assert record["ats"] == "greenhouse" and not record["apply_requested_at"]
    assert treg.stage([row["id"]])["duplicates"] == 1


def test_unknown_result_cannot_be_staged(board):  # noqa: F811
    with pytest.raises(ValueError):
        treg.stage(["a16z:elsewhere"])
    with pytest.raises(ValueError):
        treg.stage(["treg:gh:missing"])
