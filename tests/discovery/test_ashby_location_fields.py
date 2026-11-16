"""Ashby uses original source metadata but feeds only normalized strings."""

from types import SimpleNamespace

import pytest

from discovery.adapters.ashby import (
    AshbyAdapter, ashby_display_location, ashby_jd_text, ashby_location_metadata,
)
from discovery.relevance import classify_location_fit
from discovery.watchlist import CompanyConfig
from tools import posting_guard as pg


URL = "https://jobs.ashbyhq.com/example-co/synthetic-1"
BASE = {"id": "synthetic-1", "jobUrl": URL, "title": "Synthetic engineer",
        "descriptionPlain": "Remote — APAC; Office: Japan. Build example systems.",
        "descriptionHtml": "<p>Remote — APAC; Office: Japan. Build example systems.</p>"}


class FeedClient:
    def __init__(self, jobs):
        self.jobs = jobs

    def get(self, *args, **kwargs):
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"jobs": self.jobs})


def fetch(posting):
    company = CompanyConfig(name="example-co", ats="ashby", board="example-co")
    records = AshbyAdapter().fetch(company, FeedClient([posting]))
    assert len(records) == 1
    return records[0]


@pytest.mark.parametrize("primary,expected", [
    ("Vietnam", "Vietnam"),
    ({"name": "Tokyo"}, "Tokyo"),
    ({"displayName": "Japan"}, "Japan"),
    ([{"name": "Vietnam"}], "Vietnam"),
    ([{"name": "Vietnam"}, {"name": "Japan"}], ""),
    ({"name": "Japan", "displayName": "Vietnam"}, ""),
    (["Japan", "Vietnam"], ""),
    ([], ""), (None, ""), ({"unexpected": "Japan"}, ""),
])
def test_display_is_single_scalar_but_original_shape_survives(primary, expected):
    posting = {**BASE, "location": primary}
    job = fetch(posting)
    assert job.location == expected
    assert isinstance(job.location, str)
    assert job.job_id == BASE["id"] and job.jd_url == URL
    assert "{" not in job.location
    assert "Ashby location: " in job.jd_text
    assert pg.valid(job.jd_text, job.posting_guard)
    assert job.jd_text.startswith(ashby_location_metadata(posting))


def test_all_four_values_are_serialized_with_false_and_empty_array():
    posting = {**BASE, "location": {"name": "Japan", "isPrimary": True},
               "secondaryLocations": [], "workplaceType": "Remote",
               "isRemote": False}
    job = fetch(posting)
    for marker in ('Ashby location: {"isPrimary":true,"name":"Japan"}',
                   'Ashby secondaryLocations: []',
                   'Ashby workplaceType: "Remote"',
                   'Ashby isRemote: false'):
        assert marker in job.jd_text
    assert classify_location_fit(job.jd_text) == "unknown"


def test_null_and_absent_fields_do_not_create_pseudo_evidence():
    posting = {**BASE, "location": None, "secondaryLocations": None,
               "workplaceType": "Remote"}
    text = fetch(posting).jd_text
    assert "Ashby location:" not in text
    assert "Ashby secondaryLocations:" not in text
    assert "Ashby isRemote:" not in text


def test_long_description_keeps_labels_ahead_of_guard_limit():
    posting = {**BASE, "location": "Vietnam", "workplaceType": "Remote",
               "descriptionPlain": "Remote — APAC. " + "service " * 25000}
    job = fetch(posting)
    assert len(job.jd_text) == 100000
    assert job.jd_text.startswith('Ashby location: "Vietnam"')
    assert pg.valid(job.jd_text, job.posting_guard)
    assert "Remote — APAC" in job.jd_text[:200]


def test_shared_renderer_keeps_raw_metadata_separate_from_description():
    posting = {**BASE, "secondaryLocations": [{"name": "Japan"}],
               "isRemote": False}
    rendered = ashby_jd_text(posting, "Role text")
    assert rendered.endswith("\nRole text")
    assert 'Ashby secondaryLocations: [{"name":"Japan"}]' in rendered
    assert ashby_display_location([{"name": "Japan"}, {"name": "Japan"}]) == "Japan"
