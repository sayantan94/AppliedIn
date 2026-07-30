"""Applications are grouped per company, retried, and eventually given up on.

The rules encoded here are the ones with consequences. Retrying the wrong failure
sends a second application under a real name; draining one company at a time is
the pattern an employer's automation defences look for.
"""

import fakeredis
import pytest

from core import apply_queue as aq
from core.apply_queue import ApplyQueue, is_retryable


@pytest.fixture
def q():
    return ApplyQueue(fakeredis.FakeRedis(decode_responses=True))


def test_never_two_applications_to_the_same_company_at_once(q):
    """The rule the whole design exists for.

    Two sessions on one employer share a domain, a login and cookies; they
    interleave and can capture each other's page. Different employers do not
    collide, so they may run together.
    """
    q.put("waymo#1", "Waymo")
    q.put("waymo#2", "Waymo")
    q.put("netflix#1", "Netflix")

    first = q.next()
    assert first["company"] == "Waymo", "first come first served"

    second = q.next()
    assert second is not None, "a DIFFERENT company may start straight away"
    assert second["company"] == "Netflix"

    assert q.next() is None, "waymo#2 must wait: Waymo already has one running"

    q.done(first)                       # the first Waymo application finishes
    third = q.next()
    assert third is not None and third["pk"] == "waymo#2", "now it may start"


def test_a_lease_is_released_even_when_the_application_fails(q):
    """Released in a finally by the worker. A lease left behind by a crash would
    block that employer until the process restarted."""
    q.put("acme#1", "Acme")
    q.put("acme#2", "Acme")
    item = q.next()
    assert q.next() is None
    q.done(item)                        # what the worker's finally does
    assert q.next() is not None


def test_startup_clears_stale_leases(q):
    """Leases describe what is running NOW, and nothing runs in a process that has
    only just started."""
    q.put("acme#1", "Acme")
    q.put("acme#2", "Acme")
    q.next()                            # leaves Acme leased
    assert q.next() is None
    assert q.reset_leases() == 1
    assert q.next() is not None


def test_dispatch_is_first_come_first_served(q):
    """No pacing and no rotation: whatever was queued longest ago goes next."""
    import time as _t
    q.put("a#1", "Alpha", queued_at=_t.time() - 30)
    q.put("b#1", "Beta", queued_at=_t.time() - 10)
    q.put("c#1", "Gamma", queued_at=_t.time() - 20)
    assert [q.next()["company"] for _ in range(3)] == ["Alpha", "Gamma", "Beta"]


def test_an_unconfirmed_submit_is_never_retried():
    """The one that would double apply.

    "uncertain" means the form may already have gone through. Retrying it could
    submit a second application under a real name, which is the single thing this
    system must never do, so it goes to the owner instead of back to the queue.
    """
    again, _ = is_retryable({"status": "uncertain", "detail": "browser closed"})
    assert not again


@pytest.mark.parametrize("result", [
    {"status": "applied", "confirmation": "thanks"},
    {"status": "failed", "reason": "duplicate_application"},
    {"status": "failed", "reason": "guardrail"},
    {"status": "failed", "reason": "application_limit"},
    {"status": "gate", "reason": "unknown_field"},
])
def test_terminal_outcomes_are_not_retried(result):
    again, _ = is_retryable(result)
    assert not again, f"{result} must not be retried"


@pytest.mark.parametrize("result", [
    {"status": "failed", "reason": "browser_conflict"},
    {"status": "unknown", "detail": "could not read the posting"},
    {"status": "error", "reason": "timeout"},
])
def test_environment_failures_are_retried(result):
    again, _ = is_retryable(result)
    assert again, f"{result} is transient and should be retried"


def test_it_gives_up_and_dead_letters_with_its_history(q, monkeypatch):
    """A job that keeps failing has to become countable rather than a card that
    quietly stopped moving."""
    monkeypatch.setattr(aq, "BACKOFF_S", (0, 0, 0))
    q.put("acme#1", "Acme")

    item = q.next()
    for _ in range(aq.MAX_ATTEMPTS - 1):
        assert q.retry(item, "browser_conflict") is True
        q.done(item)
        item = q.next()
        assert item is not None
    assert q.retry(item, "browser_conflict") is False, "should have given up by now"

    dlq = q.dead_letters()
    assert len(dlq) == 1
    assert dlq[0]["pk"] == "acme#1"
    assert len(dlq[0]["history"]) == aq.MAX_ATTEMPTS, "each attempt should be recorded"


def test_reviving_a_dead_letter_resets_its_attempts(q, monkeypatch):
    """Asking again is a new decision, not a continuation of the failed run."""
    monkeypatch.setattr(aq, "BACKOFF_S", (0, 0, 0))
    q.put("acme#1", "Acme")
    item = q.next()
    while q.retry(item, "browser_conflict"):
        q.done(item)
        item = q.next()
    q.done(item)

    assert q.revive() == 1
    assert q.dead_letters() == []
    back = q.next()
    assert back["pk"] == "acme#1"
    assert back["attempts"] == 0, "a revived job starts its attempts over"


def test_backoff_holds_a_job_until_its_time(q, monkeypatch):
    monkeypatch.setattr(aq, "BACKOFF_S", (10_000,))
    q.put("acme#1", "Acme")
    item = q.next()
    q.retry(item, "browser_conflict")
    q.done(item)
    assert q.next() is None, "a job in backoff must not be handed out early"


def test_the_lease_records_which_job_not_just_which_company(q):
    """A company lease cannot say WHICH of two jobs at one employer is live, and
    recovery needs that: it is the difference between re-queueing a dead job and
    starting a second application on top of a running one."""
    q.put("acme#1", "Acme")
    item = q.next()
    assert q.in_flight() == {"acme#1"}
    q.done(item)
    assert q.in_flight() == set()


def test_startup_clears_the_job_leases_too(q):
    """Otherwise a pk left in the set after a crash is mistaken for a live job and
    is never reclaimed."""
    q.put("acme#1", "Acme")
    q.next()
    assert q.in_flight()
    q.reset_leases()
    assert q.in_flight() == set()


def test_the_same_job_is_not_queued_twice(q):
    """Approve-all is one button over many rows and gets clicked twice; a rescan
    re-approves rows that are already waiting. Each extra copy is dispatched and
    then refused by the duplicate guard, which spends that company's turn on a job
    that cannot run and makes the queue depth lie about the work left."""
    assert q.put("acme#1", "Acme") is True
    assert q.put("acme#1", "Acme") is False, "already waiting"
    assert q.depth()["total"] == 1

    item = q.next()                     # once it is running it is no longer pending,
    assert q.put("acme#1", "Acme") is True   # so asking again is a real request
    q.done(item)


def test_a_retry_is_never_swallowed_by_the_dedupe(q, monkeypatch):
    """retry() re-queues while the lease is STILL held — the worker releases it in a
    finally, after. Deduping against the in-flight set instead of the pending list
    would silently drop every retry and the job would vanish."""
    monkeypatch.setattr(aq, "BACKOFF_S", (0,))
    q.put("acme#1", "Acme")
    item = q.next()
    assert q.retry(item, "browser_conflict") is True, "must re-queue while leased"
    q.done(item)
    assert q.next()["pk"] == "acme#1"


def test_finishing_one_lets_the_next_at_that_company_start(q):
    """The behaviour the queue is for: a company's jobs drain one after another
    without anything else prompting them, and every one of them eventually runs."""
    for i in range(3):
        q.put(f"msft#{i}", "Microsoft")

    seen = []
    while (item := q.next()) is not None:
        assert len(set(seen) & {item["pk"]}) == 0
        seen.append(item["pk"])
        # exactly one Microsoft application is live at any moment
        assert q.depth()["running"] == ["microsoft"]
        assert q.next() is None, "a second must not start while this one runs"
        q.done(item)                    # finishing frees the next
    assert seen == ["msft#0", "msft#1", "msft#2"], "all three ran, in order"


def test_one_company_can_be_drained_on_its_own(q):
    """The manual control: work through a single employer while automatic applying
    is off, without touching anyone else's queue."""
    q.put("msft#1", "Microsoft")
    q.put("msft#2", "Microsoft")
    q.put("nv#1", "NVIDIA")

    item = q.next(only="Microsoft")
    assert item["pk"] == "msft#1"
    assert q.next(only="Microsoft") is None, "still one at a time per company"
    q.done(item)
    assert q.next(only="Microsoft")["pk"] == "msft#2", "the next one is available"

    assert q.depth()["queued"].get("nvidia") == 1, "NVIDIA was never touched"


def test_draining_one_company_still_respects_a_running_lease(q):
    """A manual run must not open a second session against a company the worker is
    already applying to — that is the invariant, not a scheduling preference."""
    q.put("msft#1", "Microsoft")
    q.put("msft#2", "Microsoft")
    live = q.next()                      # the daemon takes one
    assert q.next(only="Microsoft") is None, "by hand must not jump the lease"
    q.done(live)
    assert q.next(only="Microsoft") is not None


def test_a_manual_flush_can_be_stopped_between_jobs(q):
    """Working through a company runs its jobs back to back — four applications is
    over half an hour of browser sessions. The flag is checked BETWEEN jobs, so
    stopping is a decision not to start the next one rather than abandoning a form
    that is already half filled under a real name."""
    q.start_flush("Microsoft")
    assert q.flushing() == {"microsoft"}
    assert "microsoft" in q.depth()["flushing"], "the board must be able to show it"

    q.start_flush("Waymo")
    assert q.stop_flush("Microsoft") == 1
    assert q.flushing() == {"waymo"}, "stopping one must not stop the other"

    assert q.stop_flush() == 1, "unnamed stops everything"
    assert q.flushing() == set()


def test_no_flush_survives_a_restart(q):
    """A flush is a loop inside one process. If the flag outlived it, that company
    would look busy for ever and Process would refuse to start again."""
    q.start_flush("Microsoft")
    q.reset_leases()
    assert q.flushing() == set()


def test_claiming_a_flush_is_atomic(q):
    """Two quick clicks on Process must not start two loops against one employer.
    SADD reports whether THIS caller took it, so the second is refused."""
    assert q.start_flush("Microsoft") is True
    assert q.start_flush("Microsoft") is False, "already claimed"
    q.stop_flush("Microsoft")
    assert q.start_flush("Microsoft") is True, "claimable again once released"


def test_a_job_can_be_taken_out_of_the_queue(q):
    """"Not now" has to be possible without applying. Otherwise the only way out of
    the queue is to let it run."""
    q.put("acme#1", "Acme")
    q.put("acme#2", "Acme")
    assert q.remove("acme#1") is True
    assert q.depth()["queued"].get("acme") == 1
    assert q.next()["pk"] == "acme#2", "the survivor is untouched"


def test_removing_something_that_is_not_queued_says_so(q):
    assert q.remove("ghost#1") is False


def test_removing_the_last_job_forgets_the_company(q):
    """Otherwise the company lingers in the set and every dispatch scans an empty
    list for it."""
    q.put("acme#1", "Acme")
    q.remove("acme#1")
    assert "acme" not in q.depth()["queued"]


def test_a_running_job_cannot_be_removed_from_the_queue(q):
    """It is already leased and being filled in a browser. Deleting a queue entry
    would not stop that — it would only lose the record of what is running."""
    q.put("acme#1", "Acme")
    item = q.next()
    assert q.remove("acme#1") is False, "not pending any more"
    assert q.in_flight() == {"acme#1"}, "and still tracked as running"
    q.done(item)


def test_a_skipped_job_is_never_retried():
    """The owner said no. Retrying would argue with them."""
    again, _ = is_retryable({"status": "skipped", "reason": "user_skipped"})
    assert not again


def test_a_dead_lettered_job_can_be_forgotten_instead_of_revived(q, monkeypatch):
    monkeypatch.setattr(aq, "BACKOFF_S", (0, 0, 0))
    q.put("acme#1", "Acme")
    item = q.next()
    while q.retry(item, "browser_conflict"):
        q.done(item); item = q.next()
    q.done(item)
    assert len(q.dead_letters()) == 1
    assert q.drop_dead_letter("acme#1") == 1
    assert q.dead_letters() == []


def test_pending_returns_the_whole_queue(q):
    """The board decides a job is queued by looking for its pk in pending(). A cap
    small enough to truncate therefore does not merely hide rows: everything past
    it renders as UNQUEUED and reappears under "Ready to apply", so a company late
    in the order vanishes from the queue and looks like it was never approved.
    """
    # From 1, not 0: put() reads `queued_at or time.time()`, so a literal 0.0 is
    # taken as "not supplied" and stamped with now. No real timestamp is 0, but a
    # test that relies on it is testing the idiom rather than the queue.
    for i in range(1, 121):
        q.put(f"acme#{i}", "Acme", queued_at=float(i))
    q.put("zeta#1", "Zeta", queued_at=999.0)      # last in line

    pend = q.pending()
    assert len(pend) == 121, "every queued job must be listed"
    assert any(p["company"] == "Zeta" for p in pend), "the last company must survive"
    assert pend[-1]["pk"] == "zeta#1", "and stay last, in dispatch order"


def test_a_terminal_refusal_is_matched_by_its_CODE_not_its_prose(q):
    """The applier returns a human sentence for the card and a code for the queue.
    They were the same field, so `application_limit` never matched TERMINAL and a
    refusal that can never succeed was retried to the attempt limit: four browser
    sessions on one job the board had already declined. Every terminal outcome had
    the same fault, including duplicates and guardrail refusals.
    """
    prose = ("This EMPLOYER refused the submission under its own application cap. "
             "Nothing was submitted and nothing is wrong here")
    assert is_retryable({"status": "failed", "reason": prose})[0] is True, \
        "prose cannot be matched, which is why it must not be sent as the reason"
    assert is_retryable({"status": "failed", "reason": "application_limit"})[0] is False


def test_the_applier_returns_the_code_as_reason():
    """Pins the contract between _apply_direct and the queue."""
    import inspect

    from agent import run as _run

    src = inspect.getsource(_run._apply_direct)
    assert 'return {"result": "failed", "pk": pk, "reason": code, "detail": reason}' in src
