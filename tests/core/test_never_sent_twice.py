"""Ways one posting could be applied to twice, and the guards that stop each.

Every case here was found by reading the code, not in production, and every one
ends the same way if it comes back: a second application under a real name. They
share a cause. The duplicate guards look at a row's status and at its key, so
anything that hands a job a second key, or writes an older status back over a
newer one, walks straight past them.
"""

from __future__ import annotations

import threading
from itertools import islice
from types import SimpleNamespace

import fakeredis

from agent.run import _enqueue_apply
from core import profiles as prof
from core.apply_queue import ApplyQueue, is_retryable
from core.models import AnswerScope
from core.rotation import _dot_addresses
from core.storage.local import MarkdownAnswerBank
from discovery.chrome_crawl import _job_id, _posted_iso
from tools import seen
from tools.claude_chrome import _declares_protected, classify


class _Tracking:
    def __init__(self, rows):
        self.rows = {r["pk"]: dict(r) for r in rows}
        self.r = fakeredis.FakeRedis(decode_responses=True)

    def all(self):
        return [dict(r) for r in self.rows.values()]

    def get(self, pk):
        return dict(self.rows[pk]) if pk in self.rows else None

    def set_status(self, pk, status, **kw):
        self.rows[pk]["status"] = getattr(status, "value", status)
        self.rows[pk].update(kw)


def _stores(*rows):
    return SimpleNamespace(tracking=_Tracking(rows))


# --- a submit the page did not confirm --------------------------------------

def test_a_submit_with_no_confirmation_is_not_a_retryable_failure():
    # The task tells the session to report "blocked" when it pressed Submit and
    # then could not find a confirmation. That came back as failed/blocked, which
    # the queue retries: up to three more submissions of a form that had gone.
    out = classify({"outcome": "blocked", "submitted": True,
                    "detail": "Submitted; the page went blank."})
    assert out["status"] == "uncertain"
    assert is_retryable(out) == (False, "uncertain")


def test_a_block_before_submit_is_still_an_ordinary_failure():
    # The other half: only a pressed Submit makes it uncertain. A captcha that
    # stopped the form being filled never sent anything and may be retried.
    out = classify({"outcome": "blocked", "submitted": False, "detail": "captcha"})
    assert (out["status"], out["reason"]) == ("failed", "blocked")
    # And a refusal the page states outright keeps its own name even after a
    # submit: "5 applications per 180 days" is an answer, not an unknown.
    capped = classify({"outcome": "blocked", "submitted": True,
                       "blocked_by": "application_limit"})
    assert (capped["status"], capped["reason"]) == ("failed", "application_limit")


def test_the_pages_own_error_is_not_a_confirmation():
    out = classify({"outcome": "applied", "confirmation":
                    "Error: your application could not be submitted. Please fix the errors below."})
    assert out["status"] == "uncertain"


def test_a_report_shaped_as_a_list_keeps_its_confirmation():
    # `filled` as a list raised inside classify; the caller turned the crash into
    # "uncertain" and a confirmed application lost its record over a shape.
    out = classify({"outcome": "applied", "confirmation": "Thank you for applying",
                    "filled": [{"label": "Name", "value": "Ada"}]})
    assert out["status"] == "applied" and out["fields"] == [{"label": "Name", "value": "Ada"}]


# --- the queue ---------------------------------------------------------------

def test_a_second_gate_answer_does_not_queue_a_job_that_is_in_the_browser():
    # A double click posts the answer twice. The queue only dedupes PENDING
    # items, so the second took another slot and reset the row to `tailored`
    # under the live session.
    stores = _stores({"pk": "acme#1", "company": "Acme", "status": "submitting",
                      "jd_url": "https://acme.example/1"})
    assert _enqueue_apply("acme#1", stores)["result"] == "already_running"
    assert stores.tracking.rows["acme#1"]["status"] == "submitting"
    assert ApplyQueue(stores.tracking.r).pending() == []


def test_an_unconfirmed_submit_is_not_queued_again():
    stores = _stores({"pk": "acme#1", "company": "Acme", "status": "failed",
                      "fail_kind": "uncertain", "jd_url": "https://acme.example/1"})
    assert _enqueue_apply("acme#1", stores)["reason"] == "uncertain"
    assert stores.tracking.rows["acme#1"]["fail_kind"] == "uncertain"
    assert ApplyQueue(stores.tracking.r).pending() == []


def test_apply_as_leaves_an_unconfirmed_submit_alone(monkeypatch):
    # "Apply as" revives failed rows under the new profile. An uncertain one may
    # already be with the employer; reviving it sends a second under a new address.
    monkeypatch.setattr(prof, "bind", lambda *a: None)
    monkeypatch.setattr(prof, "retarget", lambda *a: True)
    stores = _stores(
        {"pk": "acme#1", "company": "Acme", "status": "failed", "fail_kind": "uncertain",
         "resume_tex_key": "r", "profile_id": "old"},
        {"pk": "acme#2", "company": "Acme", "status": "failed", "fail_kind": "blocked",
         "resume_tex_key": "r", "profile_id": "old"})
    p5 = prof.Profile(id="personal-5", label="Personal 5", email="me+5@x.com", phone="")
    prof.assign_company("Acme", p5, stores, ApplyQueue(stores.tracking.r))
    rows = stores.tracking.rows
    assert (rows["acme#1"]["status"], rows["acme#1"]["profile_id"]) == ("failed", "old")
    assert (rows["acme#2"]["status"], rows["acme#2"]["profile_id"]) == ("tailored", "personal-5")


# --- one posting, one key ----------------------------------------------------

def test_one_posting_found_by_two_searches_gets_one_id():
    # The crawl prompt asks for several searches merged. With the query string in
    # the id, the same posting came back as two rows and was applied to twice.
    assert (_job_id("https://x.example/jobs/123-swe?q=agentic", "SWE")
            == _job_id("https://x.example/jobs/123-swe?q=senior", "SWE") == "123-swe")
    # Where the identity IS the query, it stays.
    assert _job_id("https://x.example/apply?id=5", "SWE") == "apply?id=5"


def test_the_ledger_knows_a_posting_in_either_spelling(tmp_path, monkeypatch):
    # Career Ops, Cosign and a16z record the canonical URL; feeds compare the raw
    # one. A posting staged from one came back as new from the other.
    monkeypatch.setattr(seen, "_path", lambda: tmp_path / "seen.json")
    job = lambda url: SimpleNamespace(jd_url=url, company="Acme", title="Engineer")  # noqa: E731
    seen.mark([job("https://job-boards.greenhouse.io/acme/jobs/1")])
    again = job("https://boards.greenhouse.io/acme/jobs/1/?gh_src=abc")
    fresh = job("https://boards.greenhouse.io/acme/jobs/2")
    assert seen.unseen([again, fresh]) == [fresh]


def test_concurrent_scans_do_not_drop_each_others_postings(tmp_path, monkeypatch):
    # mark() is read, merge, write. Unlocked, the later write dropped the earlier
    # one's postings, and those then looked new on the next scan.
    monkeypatch.setattr(seen, "_path", lambda: tmp_path / "seen.json")
    urls = [f"https://x.example/jobs/{i}" for i in range(40)]
    threads = [threading.Thread(target=seen.mark, args=(
        [SimpleNamespace(jd_url=u, company="X", title="T")],)) for u in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen.load() == set(urls)


# --- what is typed into the form ---------------------------------------------

def test_the_self_identification_guard_reads_the_opening_clause():
    assert _declares_protected(
        "Disability status", "Yes, I have a disability, but I do not need an accommodation")
    assert _declares_protected("Veteran status", "Protected veteran")
    assert not _declares_protected("Disability status", "No, I do not have a disability")
    assert not _declares_protected("Veteran status", "I am not a protected veteran")
    # "race" inside another word, or about threads, is not a question about race.
    assert not _declares_protected("Do you embrace feedback?", "Yes")
    assert not _declares_protected("Have you debugged race conditions?", "Yes")


def test_a_rotated_address_never_has_two_dots_together():
    # "first.last" used to yield "first..last": not a valid address, typed into a
    # real form and recorded as used.
    for local in ("first.last", "jo.smith", "a.b"):
        assert not any(".." in a for a in islice(_dot_addresses(local), 60)), local


def test_a_multi_line_answer_survives_the_facts_file(tmp_path):
    # The file is one entry per line. A drafted essay with paragraphs came back
    # as its first line only, and the next application reused the stump.
    bank = MarkdownAnswerBank(tmp_path / "facts.md")
    essay = "First paragraph.\n\nSecond paragraph."
    bank.put("Why\nthis role?", essay, AnswerScope.COMPANY, company="Acme")
    bank.put("Notice period", "2 weeks", AnswerScope.GLOBAL)
    assert bank.lookup("Why this role?", "Acme") == essay
    assert bank.lookup("Notice period", "Acme") == "2 weeks"


# --- dates off a listing -----------------------------------------------------

def test_posted_on_is_read_as_a_date_and_an_ambiguous_one_is_not_guessed():
    assert _posted_iso("Posted on 3 Dec 2025").startswith("2025-12-03")
    # 03/04/2026 is 3 April or 4 March. A wrong guess drops a fresh role or
    # passes a stale one, so it stays undated; 25/03/2026 can only be one thing.
    assert _posted_iso("03/04/2026") == ""
    assert _posted_iso("25/03/2026").startswith("2026-03-25")


# --- two instances on one machine ---------------------------------------------

def test_each_instance_has_its_own_live_activity_channel(monkeypatch):
    # Redis keys are per database; pub/sub channels are not. With one shared
    # channel name the board on port 8788 streamed its companies, answers and
    # transcripts into the Live activity panel on 8787, and the reverse.
    from core import events
    from core.config import get_settings

    names = []
    for db in ("0", "14"):
        monkeypatch.setenv("APPLIEDIN_REDIS_URL", f"redis://localhost:6379/{db}")
        get_settings.cache_clear()
        names.append(events.channel())
    get_settings.cache_clear()
    assert names[0] != names[1]


def test_a_second_daemon_refuses_a_database_that_is_in_use(monkeypatch):
    # Ports fifteen apart derive the same database index, and a copied .env
    # points a second checkout at db 0. Two daemons on one database share a
    # board, and startup recovery resets rows the other is mid-way through.
    import socket

    import pytest

    import daemon
    from core import flags

    store = {}
    monkeypatch.setattr(flags, "get_flag", lambda k, d="": store.get(k, d))
    monkeypatch.setattr(flags, "set_flag", lambda k, v: store.__setitem__(k, v))
    with socket.socket() as other:              # stands in for the other instance
        other.bind(("127.0.0.1", 0))
        other.listen()
        store["instance_port"] = str(other.getsockname()[1])
        monkeypatch.setattr(daemon, "WEB_PORT", 1)
        with pytest.raises(SystemExit):
            daemon._claim_instance()
        assert store["instance_port"] != "1"
    # Once that instance is gone the database is free to take.
    daemon._claim_instance()
    assert store["instance_port"] == "1"
