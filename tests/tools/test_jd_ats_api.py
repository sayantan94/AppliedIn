"""The API shortcut is safe only when the response proves this exact posting and source."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from tools import jd

HTML = "<section><h2>Responsibilities</h2><p>Build reliable systems for example-co.</p></section>"
WORKDAY_URL = (
    "https://example-co.wd3.myworkdayjobs.com/en-US/External/job/US/example-role")
WORKABLE_URL = "https://apply.workable.com/example-co/j/ABC123"
EIGHTFOLD_URL = "https://example-co.eightfold.ai/careers/job/123"
PERSONIO_URL = "https://example-co.jobs.personio.com/job/123"
SMARTRECRUITERS_URL = (
    "https://jobs.smartrecruiters.com/example-co/123-platform-engineer")
WORKABLE_ENDPOINT = (
    "https://apply.workable.com/api/v1/widget/accounts/example-co?details=true")


class Response:
    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.text = payload if isinstance(payload, str) else ""
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self.payload


class Client:
    """Every api call made by the reader, for one fixed response."""

    def __init__(self, payload, seen, *, status_code=200, headers=None):
        self.payload, self.seen = payload, seen
        self.status_code, self.headers = status_code, headers

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get(self, url):
        self.seen.append(url)
        return Response(self.payload, self.status_code, self.headers)


class Clock:
    """Injected monotonic clock; sleeping advances time instead of waiting."""

    def __init__(self, now=1000.0):
        self._now = now
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self._now

    def sleep(self, delay):
        with self._lock:
            self._now += delay


@pytest.fixture(autouse=True)
def _reset_workable_state(monkeypatch):
    # The pacer is process-global; a stale reservation would make the next
    # fake-clock test sleep against real time.
    monkeypatch.setattr(jd, "_WORKABLE_NEXT_START", 0.0)


def _positive_bypass(monkeypatch, payload, url):
    seen = []
    monkeypatch.setattr(jd.httpx, "Client", lambda **_: Client(payload, seen))
    monkeypatch.setattr(jd, "_get", lambda *_: pytest.fail("API hit read the page"))
    monkeypatch.setattr(jd, "_from_chrome", lambda *_: pytest.fail("API hit used the browser"))
    result = jd.fetch_jd_meta(url)
    assert seen, "the API branch was never called"
    return result, seen


@pytest.mark.parametrize(("url", "payload", "expected"), [
    (WORKDAY_URL,
     {"jobPostingInfo": {"externalPath": "/External/job/US/example-role",
                         "jobReqId": "REQ-9", "title": "Platform Engineer",
                         "jobDescription": HTML}}, "Platform Engineer"),
    (WORKABLE_URL,
     {"jobs": [{"shortcode": "ABC123", "title": "Platform Engineer",
                 "description": HTML}]}, "Platform Engineer"),
    (EIGHTFOLD_URL,
     {"id": "123", "title": "Platform Engineer", "job_description": HTML},
     "Platform Engineer"),
    (PERSONIO_URL,
     "<workzag-jobs><position><id>123</id><name>Platform Engineer</name>"
     f"<jobDescriptions><jobDescription><![CDATA[{HTML}]]></jobDescription></jobDescriptions>"
     "</position></workzag-jobs>", "Platform Engineer"),
    (SMARTRECRUITERS_URL,
     {"id": "123", "name": "Platform Engineer",
      "postingUrl": "https://jobs.smartrecruiters.com/example-co/123-platform-engineer",
      "company": {"identifier": "example-co"},
      "jobAd": {"sections": {"jobDescription": {"text": HTML}}}},
     "Platform Engineer"),
])
def test_complete_matched_api_html_bypasses_page_and_browser(monkeypatch, url, payload, expected):
    result, seen = _positive_bypass(monkeypatch, payload, url)

    # Exactly the one detail read: no extra listing or page request.
    assert len(seen) == 1
    assert result["title"] == expected
    assert "Build reliable systems" in result["text"]
    assert result["source_kind"] == "ats_html"
    assert result["source_url"] == url
    assert result["posting_guard"]["source_verified"] is True
    assert "source_html_unavailable" not in result["posting_guard"]["reasons"]
    assert jd.pg.valid(result["text"], result["posting_guard"])
    assert jd.matched_source_witness({"jd_url": url}, result)
    # The API endpoint proves the source, never the posting's identity field.
    assert "source_identity" not in result


def test_guard_binds_the_selected_api_text(monkeypatch):
    payload = {"jobPostingInfo": {"externalPath": "/External/job/US/example-role",
                                  "title": "Platform Engineer",
                                  "jobDescription": HTML}}
    result, _ = _positive_bypass(monkeypatch, payload, WORKDAY_URL)
    assert jd.pg.valid(result["text"], result["posting_guard"])
    assert not jd.pg.valid(result["text"] + " injected", result["posting_guard"])


def test_workday_external_path_is_independent_of_job_req_id(monkeypatch):
    payload = {"jobPostingInfo": {
        "externalPath": "/External/job/US/example-role",
        "jobReqId": "REQ-9", "title": "Platform Engineer", "jobDescription": HTML}}
    result, _ = _positive_bypass(monkeypatch, payload, WORKDAY_URL)
    assert result["title"] == "Platform Engineer"
    assert "Build reliable systems" in result["text"]


@pytest.mark.parametrize("info", [
    {"jobReqId": "example-role", "title": "Platform Engineer", "jobDescription": HTML},
    {"externalPath": "/External/job/US/other", "title": "Wrong", "jobDescription": HTML},
    {"externalPath": "/External/job/US/example-role", "title": "Platform Engineer",
     "jobDescription": "Plain text role."},
])
def test_workday_unwitnessed_or_plaintext_detail_is_a_miss(monkeypatch, info):
    seen = []
    monkeypatch.setattr(jd.httpx, "Client", lambda **_: Client({"jobPostingInfo": info}, seen))
    monkeypatch.setattr(jd, "_get", lambda *_: None)
    monkeypatch.setattr(jd, "_from_chrome", lambda *_: None)
    result = jd.fetch_jd_meta(WORKDAY_URL)
    assert result.get("source_kind") != "ats_html"
    assert not result.get("posting_guard", {}).get("source_verified")


def test_workable_shortlink_is_consistent_with_the_account(monkeypatch):
    payload = {"jobs": [{"shortcode": "ABC123", "title": "Platform Engineer",
                         "shortlink": "https://apply.workable.com/j/ABC123",
                         "description": HTML}]}
    result, _ = _positive_bypass(monkeypatch, payload, WORKABLE_URL)
    assert result["source_kind"] == "ats_html"


@pytest.mark.parametrize("jobs", [
    [{"shortcode": "ABC123", "title": "One", "description": HTML},
     {"shortcode": "ABC123", "title": "Two", "description": HTML}],
    [{"shortcode": "ABC123", "title": "One",
      "url": "https://apply.workable.com/other-co/j/ABC123", "description": HTML}],
    [{"shortcode": "ZZZ999", "title": "One", "description": HTML}],
])
def test_workable_ambiguous_or_foreign_records_miss(monkeypatch, jobs):
    seen = []
    monkeypatch.setattr(jd.httpx, "Client", lambda **_: Client({"jobs": jobs}, seen))
    monkeypatch.setattr(jd, "_get", lambda *_: None)
    monkeypatch.setattr(jd, "_from_chrome", lambda *_: None)
    result = jd.fetch_jd_meta(WORKABLE_URL)
    assert result.get("source_kind") != "ats_html"


def test_workable_tenantless_shortlink_never_guesses_a_tenant(monkeypatch):
    monkeypatch.setattr(jd, "_get", lambda *_: None)
    monkeypatch.setattr(jd, "_from_chrome", lambda *_: None)
    monkeypatch.setattr(jd.httpx, "Client",
                        lambda **_: pytest.fail("tenantless shortlink reached an API"))
    assert jd._api_url("https://apply.workable.com/j/ABC123") is None
    result = jd.fetch_jd_meta("https://apply.workable.com/j/ABC123")
    assert result.get("source_kind") != "ats_html"


def test_eightfold_canonical_url_must_match_the_tenant(monkeypatch):
    payload = {"id": "123", "title": "Platform Engineer", "job_description": HTML,
               "canonicalPositionUrl": "https://other-co.eightfold.ai/careers/job/123"}
    seen = []
    monkeypatch.setattr(jd.httpx, "Client", lambda **_: Client(payload, seen))
    monkeypatch.setattr(jd, "_get", lambda *_: None)
    monkeypatch.setattr(jd, "_from_chrome", lambda *_: None)
    result = jd.fetch_jd_meta(EIGHTFOLD_URL)
    assert result.get("source_kind") != "ats_html"


def test_personio_composes_all_job_sections_in_recorded_order(monkeypatch):
    payload = (
        "<workzag-jobs><position><id>123</id><name>Platform Engineer</name>"
        "<description><![CDATA[<p>example-co is a great place.</p>]]></description>"
        "<jobDescriptions>"
        "<jobDescription><![CDATA[<h2>Responsibilities</h2><p>Build reliable systems.</p>]]>"
        "</jobDescription>"
        "<jobDescription><![CDATA[<h2>Requirements</h2><p>Five years of Python.</p>]]>"
        "</jobDescription></jobDescriptions></position></workzag-jobs>")
    result, _ = _positive_bypass(monkeypatch, payload, PERSONIO_URL)
    assert result["title"] == "Platform Engineer"
    assert result["text"].index("Build reliable systems") < result["text"].index(
        "Five years of Python")
    assert "example-co is a great place" not in result["text"]


def test_personio_intro_only_or_duplicate_id_misses(monkeypatch):
    payloads = [
        # A shared intro is not a job-specific description.
        "<workzag-jobs><position><id>123</id><name>Platform Engineer</name>"
        "<description><![CDATA[<p>example-co is a great place.</p>]]></description>"
        "</position></workzag-jobs>",
        "<workzag-jobs><position><id>123</id><name>One</name>"
        f"<jobDescriptions><jobDescription><![CDATA[{HTML}]]></jobDescription>"
        "</jobDescriptions></position>"
        "<position><id>123</id><name>Two</name>"
        f"<jobDescriptions><jobDescription><![CDATA[{HTML}]]></jobDescription>"
        "</jobDescriptions></position></workzag-jobs>",
    ]
    for payload in payloads:
        seen = []
        monkeypatch.setattr(jd.httpx, "Client", lambda p=payload, s=seen: Client(p, s))
        monkeypatch.setattr(jd, "_get", lambda *_: None)
        monkeypatch.setattr(jd, "_from_chrome", lambda *_: None)
        result = jd.fetch_jd_meta(PERSONIO_URL)
        assert result.get("source_kind") != "ats_html"


def test_smartrecruiters_keeps_role_sections_not_company_intro(monkeypatch):
    payload = {
        "id": "123", "name": "Platform Engineer",
        "postingUrl": SMARTRECRUITERS_URL,
        "company": {"identifier": "example-co"},
        "jobAd": {"sections": {
            "companyDescription": {"text": "<p>Shared example-co intro.</p>"},
            "jobDescription": {"text": "<h2>Role</h2><p>Build reliable systems.</p>"},
            "qualifications": {"text": "<h2>Requirements</h2><p>Five years of Python.</p>"},
            "additionalInformation": {"text": "<h2>Benefits</h2><p>Remote friendly.</p>"},
        }},
    }
    result, _ = _positive_bypass(monkeypatch, payload, SMARTRECRUITERS_URL)
    assert result["title"] == "Platform Engineer"
    text = result["text"]
    assert text.index("Build reliable systems") < text.index("Five years of Python")
    assert text.index("Five years of Python") < text.index("Remote friendly")
    assert "Shared example-co intro" not in text


@pytest.mark.parametrize("payload", [
    # Missing canonical posting URL.
    {"id": "123", "name": "Platform Engineer",
     "jobAd": {"sections": {"jobDescription": {"text": HTML}}}},
    # Company description is not role content.
    {"id": "123", "name": "Platform Engineer", "postingUrl": SMARTRECRUITERS_URL,
     "jobAd": {"sections": {"companyDescription": {"text": HTML}}}},
    # A different employer's detail does not witness this posting.
    {"id": "123", "name": "Platform Engineer", "postingUrl": SMARTRECRUITERS_URL,
     "company": {"identifier": "other-co"},
     "jobAd": {"sections": {"jobDescription": {"text": HTML}}}},
])
def test_smartrecruiters_unwitnessed_detail_misses(monkeypatch, payload):
    seen = []
    monkeypatch.setattr(jd.httpx, "Client", lambda **_: Client(payload, seen))
    monkeypatch.setattr(jd, "_get", lambda *_: None)
    monkeypatch.setattr(jd, "_from_chrome", lambda *_: None)
    result = jd.fetch_jd_meta(SMARTRECRUITERS_URL)
    assert result.get("source_kind") != "ats_html"


@pytest.mark.parametrize(("url", "payload"), [
    (WORKDAY_URL,
     {"jobPostingInfo": {"externalPath": "/External/job/US/other", "title": "Wrong",
                         "jobDescription": HTML}}),
    (EIGHTFOLD_URL,
     {"id": "999", "title": "Wrong", "job_description": HTML}),
    (WORKABLE_URL,
     {"jobs": [{"shortcode": "ABC123", "title": "Wrong", "description": "Plain text"}]}),
    (PERSONIO_URL,
     "<workzag-jobs><position><id>999</id><name>Wrong</name>"
     f"<jobDescriptions><jobDescription><![CDATA[{HTML}]]></jobDescription></jobDescriptions>"
     "</position></workzag-jobs>"),
    (SMARTRECRUITERS_URL,
     {"id": "123", "name": "Wrong", "postingUrl": SMARTRECRUITERS_URL,
      "jobAd": {"sections": {"jobDescription": {"text": "plain text"}}}}),
])
def test_unverified_api_payload_falls_back_without_adopting_api_metadata(
        monkeypatch, url, payload):
    seen = []
    monkeypatch.setattr(jd.httpx, "Client", lambda **_: Client(payload, seen))
    monkeypatch.setattr(jd, "_get", lambda *_: None)
    monkeypatch.setattr(jd, "_from_chrome", lambda *_: None)

    result = jd.fetch_jd_meta(url)

    assert len(seen) == 1
    assert result.get("source_kind") != "ats_html"
    assert not result.get("posting_guard", {}).get("source_verified")


@pytest.mark.parametrize("status", [301, 302, 307, 308])
@pytest.mark.parametrize("location", [
    "https://example-co.eightfold.ai/careers/job/123",
    "https://evil.test/careers/job/123",
])
def test_api_redirect_is_a_miss_without_a_redirect_hop(monkeypatch, status, location):
    seen = []
    monkeypatch.setattr(jd.httpx, "Client", lambda **_: Client(
        {}, seen, status_code=status, headers={"Location": location}))
    monkeypatch.setattr(jd, "_get", lambda *_: None)
    monkeypatch.setattr(jd, "_from_chrome", lambda *_: None)

    result = jd.fetch_jd_meta(EIGHTFOLD_URL)

    assert seen == ["https://example-co.eightfold.ai/api/apply/v2/jobs/123"]
    assert result.get("source_kind") != "ats_html"


@pytest.mark.parametrize("url", [
    "http://example-co.eightfold.ai/careers/job/123",
    "https://apply.workable.com/j/ABC123",
    "https://example-co.eightfold.ai.evil.test/careers/job/123",
    "https://example-co.eightfold.ai/careers/job/%2fetc",
    "https://example-co.eightfold.ai/careers/job/%252fetc",
    "https://user@example-co.jobs.personio.com/job/123",
    "https://jobs.smartrecruiters.com:444/example-co/123-role",
    "https://example-co.wd3.myworkdayjobs.com/en-us/External/job/US/role",
    "https://example-co.wd3.myworkdayjobs.com/External/job/../../role",
    "https://apply.workable.com/example-co/j/ABC123?x=1",
    "https://example-co.eightfold.ai/careers/job/123#frag",
    "https://a.b.eightfold.ai/careers/job/123",
    "https://a.b.jobs.personio.de/job/123",
    "https://boards.greenhouse.io/example-co/jobs/123",
    "https://jobs.lever.co/example-co/123",
    "https://jobs.ashbyhq.com/example-co/123",
])
def test_unsupported_or_ambiguous_shapes_never_create_api_destination(url):
    assert jd._api_url(url) is None


def test_short_ashby_ats_then_long_same_posting_html_keeps_selected_guard(monkeypatch):
    """#368: a short Ashby API body yields to the longer same-posting page HTML."""
    url = "https://jobs.ashbyhq.com/example-co/1234"
    short = jd._guarded("Platform Engineer", "Short role.",
                        html_variants=["<p>Short role.</p>"], source_required=True)
    short.update(source_kind="ats_html", source_url=url)
    long_html = (
        "<html><head><title>Platform Engineer | Example</title></head><body>"
        "<h1>Platform Engineer</h1><p>" + "Long role detail. " * 60 + "</p></body></html>")
    monkeypatch.setattr(jd, "_from_ats", lambda _: short)
    monkeypatch.setattr(jd, "_get", lambda *_: (long_html, url))
    monkeypatch.setattr(jd, "_from_chrome",
                        lambda *_: pytest.fail("a long page must not open the browser"))

    result = jd.fetch_jd_meta(url)

    assert "Long role detail." in result["text"]
    assert len(result["text"]) > 400
    assert result["source_kind"] == "ats_html"
    assert result["source_url"] == url
    assert result["posting_guard"]["source_verified"] is True
    assert jd.matched_source_witness({"jd_url": url}, result)


def test_workable_retry_after_parsing_uses_bounded_fallback():
    now = datetime(2026, 11, 16, tzinfo=UTC).timestamp()
    assert jd._retry_after("3", now) == 3
    assert jd._retry_after("2.5", now) == 2.5
    assert jd._retry_after("nonsense", now) == 2
    assert jd._retry_after("-1", now) == 2
    assert jd._retry_after("Sun, 16 Nov 2026 00:00:03 GMT", now) == 3
    assert jd._retry_after("Sun, 16 Nov 2026 00:00:06 GMT", now) == 6


def _limited(monkeypatch, clock, starts, retry_after):
    state = {"calls": 0}

    class C:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def get(self, url):
            state["calls"] += 1
            starts.append(clock())
            if state["calls"] == 1:
                return Response({}, status_code=429,
                                headers={"Retry-After": retry_after})
            return Response({"jobs": []})

    monkeypatch.setattr(jd, "_WORKABLE_CLOCK", clock)
    monkeypatch.setattr(jd, "_WORKABLE_SLEEP", clock.sleep)
    monkeypatch.setattr(jd.httpx, "Client", lambda **_: C())
    return state


@pytest.mark.parametrize(("retry_after", "delay"), [("2", 2.0), ("soon", 2.0)])
def test_workable_429_retry_is_bounded_and_spaced(monkeypatch, retry_after, delay):
    clock, starts = Clock(), []
    state = _limited(monkeypatch, clock, starts, retry_after)
    assert jd._workable_request(WORKABLE_ENDPOINT) == {"jobs": []}
    assert state["calls"] == 2
    assert starts[1] - starts[0] >= delay
    assert starts[1] - starts[0] <= 15.0


def test_workable_429_over_cap_falls_back_without_early_retry(monkeypatch):
    clock, starts = Clock(10.0), []
    state = _limited(monkeypatch, clock, starts, "5")
    assert jd._workable_request(WORKABLE_ENDPOINT) is None
    assert state["calls"] == 1


def test_workable_starts_are_spaced_across_accounts(monkeypatch):
    clock, starts = Clock(), []
    monkeypatch.setattr(jd, "_WORKABLE_CLOCK", clock)
    monkeypatch.setattr(jd, "_WORKABLE_SLEEP", clock.sleep)

    class C:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def get(self, url):
            starts.append(clock())
            return Response({"jobs": []})

    monkeypatch.setattr(jd.httpx, "Client", lambda **_: C())
    endpoints = [
        f"https://apply.workable.com/api/v1/widget/accounts/example-co{i}?details=true"
        for i in range(8)]
    threads = [threading.Thread(target=jd._workable_request, args=(e,)) for e in endpoints]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(starts) == len(endpoints)
    ordered = sorted(starts)
    assert all(b - a >= 1.5 for a, b in zip(ordered, ordered[1:], strict=False))


def test_workable_queued_call_beyond_its_budget_skips_the_request(monkeypatch):
    clock = Clock(1000.0)
    monkeypatch.setattr(jd, "_WORKABLE_CLOCK", clock)
    monkeypatch.setattr(jd, "_WORKABLE_NEXT_START", 1020.0)
    monkeypatch.setattr(jd.httpx, "Client",
                        lambda **_: pytest.fail("an exhausted queue must not send"))
    assert jd._workable_request(WORKABLE_ENDPOINT) is None


def test_workable_attempt_timeout_is_capped(monkeypatch):
    clock, timeouts = Clock(), []
    monkeypatch.setattr(jd, "_WORKABLE_CLOCK", clock)

    class C:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def get(self, url):
            return Response({"jobs": []})

    monkeypatch.setattr(
        jd.httpx, "Client", lambda **kwargs: (timeouts.append(kwargs), C())[1])
    assert jd._workable_request(WORKABLE_ENDPOINT) == {"jobs": []}
    assert timeouts == [{"timeout": 8.0, "follow_redirects": False}]


def test_workable_failure_does_not_leak_the_pacer_lock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(jd, "_WORKABLE_CLOCK", clock)
    monkeypatch.setattr(jd, "_WORKABLE_SLEEP", clock.sleep)

    class Boom:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def get(self, url):
            raise jd.httpx.HTTPError("boom")

    monkeypatch.setattr(jd.httpx, "Client", lambda **_: Boom())
    assert jd._workable_request(WORKABLE_ENDPOINT) is None

    monkeypatch.setattr(jd.httpx, "Client",
                        lambda **_: Client({"jobs": []}, []))
    assert jd._workable_request(WORKABLE_ENDPOINT) == {"jobs": []}


def test_non_workable_vendors_do_not_use_the_workable_pacer(monkeypatch):
    monkeypatch.setattr(jd, "_workable_start",
                        lambda *_: pytest.fail("eightfold must not pace on Workable"))
    payload = {"id": "123", "title": "Platform Engineer", "job_description": HTML}
    result, _ = _positive_bypass(monkeypatch, payload, EIGHTFOLD_URL)
    assert result["source_kind"] == "ats_html"


def test_smartrecruiters_malformed_canonical_url_is_a_miss_not_a_crash(monkeypatch):
    payload = {"id": "123", "name": "Platform Engineer",
               "postingUrl": "https://jobs.smartrecruiters.com/",
               "jobAd": {"sections": {"jobDescription": {"text": HTML}}}}
    seen = []
    monkeypatch.setattr(jd.httpx, "Client", lambda **_: Client(payload, seen))
    monkeypatch.setattr(jd, "_get", lambda *_: None)
    monkeypatch.setattr(jd, "_from_chrome", lambda *_: None)
    result = jd.fetch_jd_meta(SMARTRECRUITERS_URL)
    assert result.get("source_kind") != "ats_html"


def test_workable_short_complete_html_is_not_rejected_by_length(monkeypatch):
    clock = Clock(10.0)
    starts = []
    monkeypatch.setattr(jd, "_WORKABLE_CLOCK", clock)
    monkeypatch.setattr(jd, "_WORKABLE_SLEEP", clock.sleep)

    class ShortJobClient(Client):
        def __init__(self, **kwargs):
            self.payload = {"jobs": [{"shortcode": "ABC123", "title": "Engineer",
                                      "description": "<p>Build APIs.</p>"}]}
            self.seen = starts

        def get(self, url):
            starts.append(clock())
            return Response(self.payload)

    monkeypatch.setattr(jd.httpx, "Client", ShortJobClient)
    result = jd.fetch_jd_meta(WORKABLE_URL)
    assert result["title"] == "Engineer"
    assert result["text"] == "Build APIs."
    assert result["posting_guard"]["source_verified"] is True
    assert len(starts) == 1


@pytest.mark.parametrize(("url", "payload"), [
    (WORKDAY_URL,
     {"truncated": True,
      "jobPostingInfo": {"externalPath": "/External/job/US/example-role",
                         "title": "Platform Engineer", "jobDescription": HTML}}),
    (WORKABLE_URL,
     {"jobs": [{"shortcode": "ABC123", "title": "Platform Engineer",
                 "description": HTML}],
      "pagination": {"nextPage": 2}}),
    (EIGHTFOLD_URL,
     {"id": "123", "title": "Platform Engineer", "job_description": HTML,
      "isComplete": False}),
    (SMARTRECRUITERS_URL,
     {"id": "123", "name": "Platform Engineer", "postingUrl": SMARTRECRUITERS_URL,
      "jobAd": {"requiredSections": ["jobDescription", "qualifications"],
                "sections": {"jobDescription": {"text": HTML}}}}),
    (SMARTRECRUITERS_URL,
     {"id": "123", "name": "Platform Engineer", "postingUrl": SMARTRECRUITERS_URL,
      "jobAd": {"sections": {"jobDescription": {"text": HTML},
                             "qualifications": {"text": HTML, "partial": True}}}}),
])
def test_explicitly_incomplete_api_detail_never_verifies_source(monkeypatch, url, payload):
    """Even matching IDs and HTML cannot override response-native missing-content proof."""
    api_calls, page_calls = [], []
    monkeypatch.setattr(jd.httpx, "Client", lambda **_: Client(payload, api_calls))
    monkeypatch.setattr(jd, "_api_meta",
                        lambda *_: pytest.fail("incomplete detail reached source_complete"))
    monkeypatch.setattr(jd, "_get",
                        lambda posting, *_: (page_calls.append(posting), None)[1])
    monkeypatch.setattr(jd, "_from_chrome", lambda *_: None)

    result = jd.fetch_jd_meta(url)

    assert len(api_calls) == 1
    assert page_calls == [url, url]
    assert result.get("source_kind") != "ats_html"
    assert result.get("posting_guard", {}).get("source_verified") is not True


@pytest.mark.parametrize("wrapper", [
    "<other>{}</other>",
    "<workzag-jobs><group>{}</group></workzag-jobs>",
])
def test_personio_requires_workzag_root_and_direct_position(monkeypatch, wrapper):
    """Matching descendant IDs are not feed records unless directly under workzag-jobs."""
    position = (
        "<position><id>123</id><name>Platform Engineer</name>"
        f"<jobDescriptions><jobDescription><![CDATA[{HTML}]]></jobDescription>"
        "</jobDescriptions></position>")
    api_calls, page_calls = [], []
    monkeypatch.setattr(jd.httpx, "Client",
                        lambda **_: Client(wrapper.format(position), api_calls))
    monkeypatch.setattr(jd, "_api_meta",
                        lambda *_: pytest.fail("unsupported XML reached source_complete"))
    monkeypatch.setattr(jd, "_get",
                        lambda posting, *_: (page_calls.append(posting), None)[1])
    monkeypatch.setattr(jd, "_from_chrome", lambda *_: None)

    result = jd.fetch_jd_meta(PERSONIO_URL)

    assert len(api_calls) == 1
    assert page_calls == [PERSONIO_URL, PERSONIO_URL]
    assert result.get("source_kind") != "ats_html"
    assert result.get("posting_guard", {}).get("source_verified") is not True


def test_workable_http_date_retry_uses_wall_clock_not_pacer_clock(monkeypatch):
    """A valid UTC HTTP-date must permit a bounded retry even at low monotonic uptime."""
    monotonic, starts = Clock(500.0), []
    wall = datetime(2026, 11, 16, tzinfo=UTC).timestamp()
    monkeypatch.setattr(jd, "_WORKABLE_WALL_CLOCK", lambda: wall)
    state = _limited(monkeypatch, monotonic, starts,
                     "Mon, 16 Nov 2026 00:00:03 GMT")

    assert jd._workable_request(WORKABLE_ENDPOINT) == {"jobs": []}
    assert state["calls"] == 2
    assert starts == [500.0, 503.0]


def test_workable_queued_burst_expires_during_bounded_lock_acquisition(monkeypatch):
    """24 queued callers must time out within their own budgets, not issue HTTP."""
    clock = Clock()
    arrivals = threading.Barrier(25)

    class SaturatedLock:
        def __init__(self):
            self.waits = []
            self._mutex = threading.Lock()

        def acquire(self, *, timeout):
            # A fake occupied pacer lock: expire the provided budget virtually,
            # not via a 15-second real sleep for every concurrent worker.
            with self._mutex:
                self.waits.append(timeout)
            clock.sleep(timeout)
            return False

        def release(self):
            raise AssertionError("a timed-out caller must not release another holder's lock")

    lock = SaturatedLock()
    monkeypatch.setattr(jd, "_WORKABLE_CLOCK", clock)
    monkeypatch.setattr(jd, "_WORKABLE_LOCK", lock)
    monkeypatch.setattr(jd.httpx, "Client",
                        lambda **_: pytest.fail("expired queue sent an HTTP request"))
    results, errors = [], []

    def worker(index):
        try:
            arrivals.wait()
            endpoint = (f"https://apply.workable.com/api/v1/widget/accounts/"
                        f"example-co{index}?details=true")
            results.append(jd._workable_request(endpoint))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(24)]
    for thread in threads:
        thread.start()
    arrivals.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert results == [None] * 24
    assert len(lock.waits) == 24
    assert all(0 < budget <= 15.0 for budget in lock.waits)


def test_three_part_workday_native_detail_carries_only_matched_company(monkeypatch):
    url = "https://example-co.wd3.myworkdayjobs.com/example/job/ROLE-1"
    payload = {"jobPostingInfo": {
        "externalPath": "/example/job/ROLE-1", "jobReqId": "DIFFERENT-REQ",
        "title": "Platform Engineer", "companyName": "Example Co",
        "jobDescription": HTML}}
    result, seen = _positive_bypass(monkeypatch, payload, url)
    assert len(seen) == 1
    assert result["title"] == "Platform Engineer"
    assert result["company"] == "Example Co"
    assert result["source_kind"] == "ats_html"
    assert jd.matched_source_witness({"jd_url": url}, result)


def test_three_part_workday_mismatched_external_path_uses_page_not_api(monkeypatch):
    url = "https://example-co.wd3.myworkdayjobs.com/example/job/ROLE-1"
    payload = {"jobPostingInfo": {
        "externalPath": "/example/job/OTHER-1", "title": "Foreign Title",
        "companyName": "Foreign Co", "jobDescription": HTML}}
    seen = []
    monkeypatch.setattr(jd.httpx, "Client", lambda **_: Client(payload, seen))
    html = ("<html><body><h1>Platform Engineer</h1><p>" +
            "Synthetic source-matched role with reliable integrations. " * 12 +
            "</p></body></html>")
    monkeypatch.setattr(jd, "_get", lambda *_: (html, url))
    monkeypatch.setattr(jd, "_from_chrome",
                        lambda *_: pytest.fail("complete HTML should avoid browser"))

    result = jd.fetch_jd_meta(url)
    assert len(seen) == 1
    assert result["source_kind"] == "http_html"
    assert result["title"] == "Platform Engineer"
    assert result.get("company") != "Foreign Co"
    assert jd.matched_source_witness({"jd_url": url}, result)


@pytest.mark.parametrize("url", [
    "https://example-co.wd3.myworkdayjobs.com/example/job/ROLE-1",
    "https://example-co.wd3.myworkdayjobs.com/en-US/example/job/ROLE-1",
    "https://example-co.wd3.myworkdayjobs.com/example/job/US/ROLE-1",
    "https://example-co.wd3.myworkdayjobs.com/en-US/example/job/US/ROLE-1",
])
def test_workday_short_long_and_locale_forms_keep_exact_external_path(url):
    parsed = jd._api_url(url)
    assert parsed and parsed[0] == "workday"
    assert parsed[2] == "/" + url.split(".com/", 1)[1].removeprefix("en-US/")
    assert parsed[3].endswith(parsed[2])
