"""Applications are grouped per company, retried, and eventually given up on.

The rules encoded here are the ones with consequences. Retrying the wrong failure
sends a second application under a real name; draining one company at a time is
the pattern an employer's automation defences look for.
"""

import json

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


def test_the_apply_paths_own_result_key_is_understood():
    """`_apply_direct` returns its outcome under `result`, not `status`.

    is_retryable read only `status`, so every real apply outcome arrived as "" and
    fell through to (False, "done"). The retry branch was unreachable: no timeout,
    no unreachable posting, no environment fault was ever retried, backed off or
    dead lettered. Live corroboration when this was found — 31 rows in Unable,
    zero "apply retry" lines in a 29k line log, and an empty dead letter queue.
    """
    assert is_retryable({"result": "failed", "reason": "timeout"}) == (True, "timeout")
    assert is_retryable({"result": "error", "reason": "jd_fetch_failed"})[0] is True
    # The successful and gated outcomes use their own words on this path.
    assert is_retryable({"result": "done", "confirmation": "x"})[0] is False
    assert is_retryable({"result": "gated", "question": "?"})[0] is False
    # The engines' own vocabulary still works — one shape must not break the other.
    assert is_retryable({"status": "failed", "reason": "timeout"}) == (True, "timeout")
    assert is_retryable({"status": "applied"})[0] is False


def test_an_uncertain_apply_is_never_retried(q):
    """The one outcome that must never be tried again.

    `uncertain` means the browser died partway and the form MAY already have been
    submitted; a second attempt sends a second application under a real name. It
    carries no `reason`, so the applier used to record it as the code "failed" —
    which is not in TERMINAL. That was survivable only while retries were dead.
    With the key fixed, this is the assertion standing between one application
    and two, so it is pinned at both layers.
    """
    # The queue refuses it when the code survives.
    assert is_retryable({"result": "failed", "reason": "uncertain"})[0] is False
    assert is_retryable({"status": "uncertain"})[0] is False

    # And the applier must send the status as the code when there is no reason,
    # rather than flattening it to "failed".
    import inspect

    from agent import run as _run

    src = inspect.getsource(_run._apply_direct)
    assert 'code = str(result.get("reason") or result.get("status") or "failed")' in src, \
        "an uncertain outcome must reach the queue as 'uncertain', not as 'failed'"


def test_every_backoff_tier_is_reachable():
    """A tier nobody ever waits is a lie in the config.

    retry() dead letters at attempts >= MAX_ATTEMPTS, so the wait index only ever
    reaches MAX_ATTEMPTS - 2. A third 30 minute entry read as policy and was not.
    """
    from core.apply_queue import BACKOFF_S, MAX_ATTEMPTS

    highest_index_used = min(MAX_ATTEMPTS - 2, len(BACKOFF_S) - 1)
    assert highest_index_used == len(BACKOFF_S) - 1, (
        f"BACKOFF_S has {len(BACKOFF_S)} tiers but only "
        f"{highest_index_used + 1} can ever be waited")


def test_an_outage_does_not_spend_the_job_s_attempts(q):
    """Three attempts exist to stop a job that cannot be applied to from being
    tried forever. A signed-out CLI says nothing about the job, and counting it
    dead-lettered a real OpenAI role that had never had a browser opened for it:
    two attempts on an expired login, a third on a daemon restart."""
    infra = ("The Claude CLI is signed out, so the browser session could not "
             "start. Nothing was submitted.")
    item = {"pk": "openai#1", "company": "OpenAI", "attempts": 0}
    for _ in range(5):
        assert q.retry(item, infra) is True, "an outage must not dead-letter a job"
        item = next(i for i in q.pending() if i["pk"] == "openai#1")
        q.remove("openai#1")
        item = {**item, "history": item.get("history") or []}
    assert int(item.get("attempts", 0)) == 0


def test_a_real_failure_still_spends_them(q):
    """The budget still exists — it is only outages that are exempt."""
    item = {"pk": "acme#1", "company": "Acme", "attempts": 0}
    assert q.retry(item, "the form could not be filled") is True
    got = next(i for i in q.pending() if i["pk"] == "acme#1")
    assert got["attempts"] == 1


# --- the actionable view of pending() ----------------------------------------
def _row(q, pk, status):
    q.r.set(f"app:{pk}", json.dumps({"pk": pk, "status": status}))


_HIDDEN = ("applied", "applied_manual", "skipped", "job_gone")
_KEPT = ("failed", "error", "capped", "found", "tailoring", "tailored",
         "submitting", "needs_human", "some_future_status")


def test_pending_hides_only_the_four_finished_statuses(q):
    n = 0
    expected = set()
    for status in _HIDDEN + _KEPT:
        n += 1
        q.put(f"co{n}#1", f"Co{n}", queued_at=float(n))
        _row(q, f"co{n}#1", status)
        if status in _KEPT:
            expected.add(f"co{n}#1")
    # Rows that prove nothing about the job stay visible: absent, malformed,
    # not an object, and an object with no status.
    q.put("none#1", "NoRow", queued_at=100.0)
    q.put("bad#1", "Bad", queued_at=101.0)
    q.r.set("app:bad#1", "{not json")
    q.put("list#1", "ListRow", queued_at=102.0)
    q.r.set("app:list#1", "[]")
    q.put("nostatus#1", "NoStatus", queued_at=103.0)
    q.r.set("app:nostatus#1", json.dumps({"pk": "nostatus#1"}))
    q.put("lst#1", "ListStatus", queued_at=104.0)       # unhashable status values
    q.r.set("app:lst#1", json.dumps({"pk": "lst#1", "status": []}))
    q.put("obj#1", "ObjStatus", queued_at=105.0)
    q.r.set("app:obj#1", json.dumps({"pk": "obj#1", "status": {}}))
    expected |= {"none#1", "bad#1", "list#1", "nostatus#1", "lst#1", "obj#1"}

    assert {i["pk"] for i in q.pending(actionable=True)} == expected
    # The physical queue is untouched: nothing was deleted or made dispatchable.
    assert q.depth()["total"] == len(_HIDDEN) + len(_KEPT) + 6
    # The default stays the physical queue: guards and drains must see everything.
    assert len(q.pending()) == len(_HIDDEN) + len(_KEPT) + 6


def test_filtered_pending_keeps_order_rank_backoff_and_busy(q):
    import time as _t
    now = _t.time()
    q.put("acme#1", "Acme", queued_at=1.0)
    _row(q, "acme#1", "applied")                    # oldest, finished
    q.put("acme#2", "Acme", queued_at=2.0)
    q.put("acme#3", "Acme", queued_at=3.0, not_before=now + 500)
    _row(q, "acme#3", "failed")                     # a retry waiting out its backoff
    q.put("beta#1", "Beta", queued_at=2.5)
    _row(q, "beta#1", "capped")                     # parked, still waiting
    q.r.sadd("applyq:inflight", "beta")

    got = q.pending(actionable=True)
    assert [i["pk"] for i in got] == ["acme#2", "beta#1", "acme#3"]
    by = {i["pk"]: i for i in got}
    assert by["acme#2"]["company_rank"] == 1, "rank counts survivors only"
    assert by["acme#3"]["company_rank"] == 2
    assert by["acme#3"]["blocked"] == "backoff" and by["acme#3"]["ready_in"] > 0
    assert by["beta#1"]["blocked"] == "company_busy"


def test_the_limit_applies_after_the_filter(q):
    for i in range(1, 6):
        q.put(f"old#{i}", "Old", queued_at=float(i))
        _row(q, f"old#{i}", "job_gone")
    for i in range(6, 9):
        q.put(f"live#{i}", "Live", queued_at=float(i))

    assert [i["pk"] for i in q.pending(limit=2, actionable=True)] == ["live#6", "live#7"]


class _CountingRedis(fakeredis.FakeRedis):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.mgets = 0

    def get(self, *a, **kw):
        raise AssertionError("one GET per queue entry is forbidden")

    def mget(self, *a, **kw):
        self.mgets += 1
        return super().mget(*a, **kw)


def test_a_large_queue_is_filtered_in_a_few_batched_reads():
    r = _CountingRedis(decode_responses=True)
    q = ApplyQueue(r)
    for i in range(1, 1171):
        q.r.rpush("applyq:co:acme", json.dumps(
            {"pk": f"acme#{i}", "company": "Acme", "queued_at": float(i)}))
        r.set(f"app:acme#{i}", json.dumps(
            {"pk": f"acme#{i}", "status": "applied" if i <= 1070 else "tailored"}))
    r.sadd("applyq:companies", "acme")

    got = q.pending(actionable=True)
    assert [i["pk"] for i in got] == [f"acme#{i}" for i in range(1071, 1171)]
    assert 1 <= r.mgets <= 3


def test_an_unreadable_status_read_hides_nothing(q):
    q.put("acme#1", "Acme", queued_at=1.0)
    _row(q, "acme#1", "applied")

    def boom(keys):
        raise ConnectionError("redis went away")

    q.r.mget = boom
    assert [i["pk"] for i in q.pending(actionable=True)] == ["acme#1"]
