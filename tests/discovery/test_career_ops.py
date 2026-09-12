"""A public search result cannot silently become an approved application."""

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import fakeredis
import pytest
from fastapi import BackgroundTasks

from core.models import JobRecord, Status
from core.storage.local import RedisTracking
from discovery import career_ops as co
from discovery import career_ops_api as api


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(co, "_path", lambda: tmp_path / "board.json")
    monkeypatch.setattr(
        co,
        "catalog",
        lambda: [
            {"name": "Acme", "provider": "ashby", "tracked": True},
            {"name": "Beta", "provider": "ashby", "tracked": False},
        ],
    )
    stores = SimpleNamespace(
        tracking=RedisTracking(fakeredis.FakeRedis(decode_responses=True)),
        queue=Mock(),
        tailor_queue="tailor",
        apply_queue="apply",
    )
    monkeypatch.setattr(co, "make_stores", lambda: stores)
    monkeypatch.setattr("core.flags.paused", lambda: False)
    monkeypatch.setattr("core.flags.company_filter", lambda company: [])
    monkeypatch.setattr("discovery.crawler._age_limit", lambda company: 0)
    data = co._read()
    rows = [
        co.normalize(
            {
                "url": f"https://jobs.ashbyhq.com/{name.lower()}/{i}",
                "title": "Engineer",
                "description": "A complete posting body",
                "location": "San Francisco",
                "postedAt": 1730477203318,
            },
            name,
            "ashby",
        )
        for name, i in [("Acme", "one"), ("Acme", "two"), ("Beta", "three")]
    ]
    data["jobs"] = {r["id"]: r for r in rows}
    co._write(data)
    yield stores, rows
    co._RUNNING = False
    co._ACTIVE = {}
    if co._SCAN.locked():
        co._SCAN.release()


def test_prepare_is_exact_idempotent_and_stamps_approval_guard_before_worker_can_read(board):
    stores, rows = board

    def enqueue(queue, message):
        row = stores.tracking.get(message["pk"])
        assert queue == "tailor"
        assert row["discovery_source"] == "career_ops"
        assert row["jd_text"] == "A complete posting body"

    stores.queue.enqueue.side_effect = enqueue
    result = co.prepare([rows[0]["id"]])
    assert result["prepared"] == 1
    assert stores.tracking.get("acme#one")["status"] == "found"
    assert stores.tracking.get("acme#two") is None
    assert co.prepare([rows[0]["id"]])["duplicates"] == 1
    assert stores.queue.enqueue.call_count == 1


def test_existing_application_and_seen_url_are_never_reimported(board):
    stores, rows = board
    stores.tracking.put_new(
        JobRecord(
            company="Acme",
            job_id="original",
            title="Engineer",
            jd_url=rows[0]["url"] + "?utm_source=other",
            jd_text="",
        )
    )
    stores.tracking.set_status("acme#original", Status.APPLIED, confirmation_id="confirmed")
    from tools import seen

    seen.mark([SimpleNamespace(jd_url=rows[1]["url"], company="Acme", title="Engineer")])
    result = co.prepare([rows[0]["id"], rows[1]["id"]])
    assert result["prepared"] == 0 and result["duplicates"] == 2
    assert stores.tracking.get("acme#original")["confirmation_id"] == "confirmed"
    assert stores.tracking.get("acme#one") is None
    stores.queue.enqueue.assert_not_called()


def test_invalid_batch_changes_nothing(board):
    stores, rows = board
    with pytest.raises(ValueError):
        co.prepare([rows[0]["id"], "unknown"])
    assert not stores.tracking.all()


def test_rescan_keeps_dismissal_and_first_seen_and_reports_partial_failures(board, monkeypatch):
    stores, rows = board
    co.dismiss([rows[0]["id"]])
    monkeypatch.setattr(
        co,
        "_bridge",
        lambda payload: [
            {
                "company": "Acme",
                "provider": "ashby",
                "jobs": [{"url": rows[0]["url"], "title": "Engineer II"}],
            },
            {"company": "Beta", "error": "HTTP 503"},
        ],
    )
    assert co.reserve_scan()
    assert not co.reserve_scan()
    co.scan_reserved()
    data = co._read()
    assert data["jobs"][rows[0]["id"]]["state"] == "dismissed"
    assert data["jobs"][rows[0]["id"]]["first_seen"] == rows[0]["first_seen"]
    assert data["jobs"][rows[0]["id"]]["title"] == "Engineer II"
    assert data["last_scan"]["found"] == 1 and data["last_scan"]["errors"]
    assert not co._RUNNING
    stores.queue.enqueue.assert_not_called()


def test_company_search_does_not_fall_back_to_all_sources(board, monkeypatch):
    payloads = []
    monkeypatch.setattr(co, "_bridge", lambda p: payloads.append(p) or [])
    assert co.reserve_scan()
    co.scan_reserved("Beta")
    assert [c["name"] for c in payloads[0]["entries"]] == ["Beta"]
    background = BackgroundTasks()
    result = api.scan(api.ScanScope(company="Unknown"), background)
    assert result["kind"] == "web"
    assert background.tasks[0].func is co.search_reserved
    assert background.tasks[0].args == ("Unknown",)


def test_sources_and_automation_round_trip_and_missing_sources_do_not_look_saved(board):
    values = {"companies": ["Beta"], "scheduled": False, "auto_prepare": True}
    assert co.configure(values) == values
    assert co.snapshot()["settings"] == values
    with pytest.raises(ValueError):
        co.configure({**values, "companies": ["Unknown"]})
    assert co.snapshot()["settings"] == values


def test_timeouts_keep_completed_company_results(board, monkeypatch):
    line = json.dumps({"company": "Acme", "jobs": []}) + "\n"

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 600, output=(line + '{"company":').encode())

    monkeypatch.setattr(co.subprocess, "run", timeout)
    results = co._bridge({"entries": []})
    assert results[0] == {"company": "Acme", "jobs": []}
    assert "timed out" in results[-1]["error"]


def test_paused_daemon_cannot_schedule_or_auto_prepare(board, monkeypatch):
    stores, rows = board
    monkeypatch.setattr("core.flags.paused", lambda: True)
    assert co.auto_prepare(stores, "", {"Acme"}) == 0
    reserve = Mock()
    monkeypatch.setattr(co, "reserve_scan", reserve)
    co.scheduled_tick()
    reserve.assert_not_called()


def test_automatic_preparation_scopes_company_and_excludes_stale_results(board, monkeypatch):
    stores, rows = board
    data = co._read()
    data["jobs"][rows[1]["id"]]["last_seen"] = "2020-01-01T00:00:00+00:00"
    co._write(data)
    monkeypatch.setattr("discovery.relevance.relevant", lambda jobs, prefs: jobs)
    from discovery.watchlist import Preferences

    monkeypatch.setattr("discovery.watchlist.load_preferences", lambda p: Preferences())
    monkeypatch.setattr("core.flags.effective_prefs", lambda company, base: base)
    assert co.auto_prepare(stores, "2026-01-01", {"Acme"}) == 1
    assert stores.tracking.get("acme#one")
    assert stores.tracking.get("acme#two") is None
    assert stores.tracking.get("beta#three") is None


def test_imported_jobs_still_stop_for_review_under_global_auto_mode(board, monkeypatch):
    stores, rows = board
    co.prepare([rows[0]["id"]])
    from agent import run

    monkeypatch.setattr("core.flags.apply_mode", lambda: "auto")
    stores.tracking.set_status("acme#one", Status.FOUND, match_score=10)
    assert run._auto_decision("acme#one", stores) == ""
    monkeypatch.setattr(run, "_claim", lambda *a: True)
    monkeypatch.setattr(run, "_release", lambda *a: None)

    async def drive(pk, row, stores, *, prepare_only=False):
        assert prepare_only is True, "Restarted workers must use the review-only graph"
        return {"result": "prepared"}

    monkeypatch.setattr(run, "_run_job_async", drive)
    assert run.run_job("acme#one", stores)["result"] == "prepared"


def test_normalization_rejects_unsafe_links_and_preserves_identifying_queries():
    assert co.normalize({"url": "javascript:alert(1)", "title": "Engineer"}, "A", "ashby") is None
    assert (
        co.canonical("https://boards.greenhouse.io/acme/jobs/1/?gh_src=x")
        == "https://job-boards.greenhouse.io/acme/jobs/1"
    )
    assert "job=12" in co.canonical("https://acme.com/careers?job=12&utm_source=x")


def test_provider_identity_is_kept_when_the_url_also_contains_a_title():
    row = co.normalize(
        {
            "id": "1234",
            "url": "https://jobs.smartrecruiters.com/Acme/1234-engineer",
            "title": "Engineer",
        },
        "Acme",
        "smartrecruiters",
    )
    assert row["job_id"] == "1234", "Native discovery uses the API ID, without the title slug"


def test_local_source_correction_replaces_stale_upstream_api(tmp_path, monkeypatch):
    repo = tmp_path / "upstream"
    (repo / "providers").mkdir(parents=True)
    (repo / "providers/_http.mjs").touch()
    (repo / "templates").mkdir()
    (repo / "templates/portals.example.yml").write_text(
        "tracked_companies:\n  - name: Acme\n    api: https://old.example/jobs\n"
    )
    config = tmp_path / "config"
    config.mkdir()
    (config / "watchlist.yaml").write_text("companies:\n  - name: Acme\n")
    (config / "career_ops_sources.yaml").write_text(
        "companies:\n  - name: Acme\n    careers_url: https://jobs.ashbyhq.com/acme\n"
    )
    monkeypatch.setattr(co.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=co.REVISION))
    monkeypatch.setattr(co, "_bridge", lambda p, **kw: p["entries"])
    sources = co._catalog_at(str(repo), str(config))
    assert sources[0]["careers_url"] == "https://jobs.ashbyhq.com/acme"
    assert "api" not in sources[0], (
        "Keeping the stale API would silently override the corrected URL"
    )


def test_automatic_preparation_honors_saved_age_window_before_calling_model(board, monkeypatch):
    stores, rows = board
    monkeypatch.setattr("discovery.crawler._age_limit", lambda company: 24)
    from discovery.watchlist import Preferences

    monkeypatch.setattr("discovery.watchlist.load_preferences", lambda p: Preferences())
    monkeypatch.setattr("core.flags.effective_prefs", lambda company, base: base)
    screen = Mock(return_value=[])
    monkeypatch.setattr("discovery.relevance.relevant", screen)
    assert co.auto_prepare(stores, "2026-01-01", {"Acme"}) == 0
    assert screen.call_args.args[0] == [], "Old postings must not cost a model call"
    stores.queue.enqueue.assert_not_called()


def test_interest_search_adds_new_companies_without_enqueuing_applications(board, monkeypatch):
    stores, rows = board
    monkeypatch.setattr(co, "search_preferences", lambda company="": {"titles": ["Engineer"]})
    monkeypatch.setattr(
        "discovery.interest_search.search",
        lambda *args, **kwargs: {
            "jobs": [
                {
                    "company": "New Employer",
                    "title": "Engineer",
                    "url": "https://jobs.ashbyhq.com/new/123",
                    "description": "A snippet is not a job description",
                    "why": "Developer tools",
                    "verification": "needs_posting_read",
                }
            ],
            "summary": "One match",
            "queries": ["developer tools"],
        },
    )
    assert co.reserve_scan("interests")
    co.search_reserved(interests="Developer tools")
    data = co._read()
    result = next(r for r in data["jobs"].values() if r["company"] == "New Employer")
    assert result["description"] == ""
    assert result["search_id"] == data["last_search"]["started_at"]
    assert data["last_search"]["found"] == 1
    stores.queue.enqueue.assert_not_called()
    assert co.prepare([result["id"]])["prepared"] == 1
    assert stores.tracking.get("new employer#123")["jd_text"] == ""
    assert stores.tracking.get("new employer#123")["discovery_source"] == "career_ops"


def test_search_failure_releases_lock_and_preserves_existing_board(board, monkeypatch):
    _, rows = board
    monkeypatch.setattr(co, "search_preferences", lambda company="": {})

    def fail(*args, **kwargs):
        raise ValueError("Search provider unavailable")

    monkeypatch.setattr("discovery.interest_search.search", fail)
    assert co.reserve_scan("interests")
    co.search_reserved()
    assert len(co._read()["jobs"]) == len(rows)
    assert "unavailable" in co._read()["last_search"]["errors"][0]["error"]
    assert not co._SCAN.locked() and not co._RUNNING


def test_progress_is_bounded_and_new_run_does_not_replay_old_search(board):
    assert co.reserve_scan("interests")
    first = co.progress_snapshot()["run_id"]
    for i in range(100):
        co.report_progress(f"Searched: query {i}")
    progress = co.progress_snapshot()
    assert len(progress["events"]) == 80
    assert progress["events"][-1]["message"] == "Searched: query 99"
    progress["events"].clear()
    assert len(co.progress_snapshot()["events"]) == 80
    co._SCAN.release()
    assert co.reserve_scan("interests")
    assert co.progress_snapshot()["run_id"] != first
    assert len(co.progress_snapshot()["events"]) == 1


async def test_progress_stream_replays_then_finishes_when_search_ends(board, monkeypatch):
    assert co.reserve_scan("interests")
    co.report_progress("Checking 2 posting links…")
    response = await api.progress()
    events = response.body_iterator
    first = json.loads((await anext(events)).removeprefix("data: "))
    assert first["running"] is True
    assert first["events"][-1]["message"] == "Checking 2 posting links…"
    co._RUNNING = False
    last = json.loads((await anext(events)).removeprefix("data: "))
    assert last["running"] is False
    with pytest.raises(StopAsyncIteration):
        await anext(events)
