"""Issue #400: a BrowserSkill outage during a JD read is never terminal no_jd.

Observed live (2026-11-21): a Workday posting answered HTTP 200 with a
near-empty script shell, the BrowserSkill fallback refused with
`permission_denied`, the refusal was swallowed and the row closed as no_jd.
Retry of that row re-ran the same swallow and logged nothing. Offline, synthetic.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis
import pytest

from agent import run
from core.models import JobRecord, Status
from core.storage.local import RedisTracking
from tools import jd

PK = "example-co#job-1"
URL = "https://example-co.wd5.myworkdayjobs.com/en-US/careers/job/Remote/Engineer_R-1"
# The shape a Workday page serves over plain HTTP: title, scripts, no body text.
SHELL = ("<html><head><title>Workday</title><script>window.workday={}</script>"
         "</head><body><div id='root'></div></body></html>")
POSTING = "Responsibilities: build reliable distributed systems for customers. " * 15


@pytest.fixture(autouse=True)
def isolate_reader(monkeypatch):
    monkeypatch.setattr(jd, "_browser_retry", (0, ""))
    monkeypatch.setattr(jd, "_from_new_ats", lambda url: None)
    monkeypatch.setattr(jd, "_from_ats", lambda url: None)
    monkeypatch.setattr(jd, "_get", lambda url, headers: (SHELL, url))
    monkeypatch.setattr("core.events.emit", lambda *a, **kw: None)


def _refusing_browser(monkeypatch, code):
    calls = []

    def refuse(url, kind="jd"):
        calls.append(url)
        raise jd.PostingReadUnavailable(f"BrowserSkill operation unavailable: {code}")

    monkeypatch.setattr(jd, "_from_chrome", refuse)
    return calls


def _stores(**fields):
    tracking = RedisTracking(fakeredis.FakeRedis(decode_responses=True))
    assert tracking.put_new(JobRecord(company="example-co", job_id="job-1",
                                      title="Engineer", jd_url=URL, jd_text=""))
    if fields:
        tracking.set_status(PK, fields.pop("status"), **fields)
    return SimpleNamespace(tracking=tracking)


@pytest.mark.parametrize("code", ["permission_denied", "cdp_failed"])
def test_short_http_shell_with_browser_outage_raises(monkeypatch, code):
    calls = _refusing_browser(monkeypatch, code)
    with pytest.raises(jd.PostingReadUnavailable, match=code):
        jd.fetch_jd_meta(URL, "jd_sweep")
    assert calls == [URL]


@pytest.mark.parametrize("code", ["permission_denied", "cdp_failed"])
def test_browser_outage_takes_attempts_path_not_no_jd(monkeypatch, code):
    _refusing_browser(monkeypatch, code)
    monkeypatch.setattr(run, "_session_service",
                        lambda: pytest.fail("No tailoring without a posting"))
    stores = _stores()

    result = run.run_job(PK, stores, prepare_only=True)

    row = stores.tracking.get(PK)
    assert result["result"] == "deferred"
    assert result["reason"] == "jd_reader_unavailable"
    assert row["status"] == Status.FOUND.value
    assert row.get("fail_kind") in ("", None)
    assert row["jd_read_attempts"] == 1
    assert code in row["jd_read_error"]


def test_retry_of_no_jd_row_rereads_posting_and_logs(monkeypatch, caplog):
    stores = _stores(status=Status.FAILED, fail_kind="no_jd",
                     fail_reason="Could not read the posting")
    reads = []

    def read(url, kind="jd"):
        reads.append(url)
        return jd._guarded("Engineer", POSTING)

    monkeypatch.setattr(jd, "_from_chrome", read)
    monkeypatch.setattr(run, "_reset_session", AsyncMock())
    # Stop right after the read: preparation itself is not under test here.
    monkeypatch.setattr(run, "_eligibility_conflict", lambda *a: "synthetic stop")
    monkeypatch.setattr(run, "_session_service",
                        lambda: pytest.fail("stopped before tailoring"))

    with caplog.at_level(logging.INFO):
        result = run.retry_job(PK, stores)

    row = stores.tracking.get(PK)
    assert reads == [URL]
    assert row.get("fail_kind") != "no_jd"
    assert row["jd_text"].strip() == POSTING.strip()
    assert any(f"retry: pk={PK} result={result['result']}" in r.getMessage()
               for r in caplog.records)
