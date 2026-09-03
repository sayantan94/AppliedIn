"""Re-tailor one job with the owner's own guidance, without applying anything.

The owner reads a tailored résumé, sees it under-plays the thing this JD is
actually about, and has no way to say so. `retry_job` re-runs the WHOLE pipeline
including the applier, so using it to adjust a résumé submits an application.

So this runs the tailor alone. Two rules make it safe to expose as a button:

  applied rows are refused   that document went to an employer, and rewriting the
                             row would present something they never received as
                             what was sent.
  the note steers EMPHASIS   `save_tailored_resume` still demands every employer,
                             title and date line verbatim and still refuses a
                             dropped bullet, so "lean into Kubernetes" can reorder
                             and reword what is there and cannot add what is not.

The note is stored on the row rather than passed once. Editing base.tex makes
every tailored row stale, and the stale-résumé guard re-tailors a row before it
applies — a transient note would be silently thrown away by that re-tailor and
the résumé the owner had just corrected would go out uncorrected.
"""

from __future__ import annotations

import pytest

from agent import run as R


class _Tracking:
    def __init__(self, row):
        self.row = dict(row)

    def get(self, pk):
        return dict(self.row) if self.row.get("pk") == pk else None

    def set_status(self, pk, status, **kw):
        self.row["status"] = getattr(status, "value", status)
        self.row.update(kw)


class _Stores:
    def __init__(self, row):
        self.tracking = _Tracking(row)


@pytest.fixture
def ran(monkeypatch):
    """Capture the session state the tailor would have been given."""
    seen = {}

    async def _fake(pk, state, title, company):
        seen.update(state=state, pk=pk, title=title, company=company)

    monkeypatch.setattr(R, "_tailor_only", _fake)
    monkeypatch.setattr(R, "_base_latex", lambda: "\\begin{document}seed\\end{document}")
    monkeypatch.setattr(R, "_github_context", lambda: "")
    monkeypatch.setattr(R, "_prefs_notes", lambda: "")
    return seen


def _row(**kw):
    return {"pk": "stripe#1", "company": "Stripe", "title": "SWE",
            "status": "tailored", "jd_url": "https://x", "ats": "greenhouse",
            "jd_text": "We want Kubernetes and distributed systems. " * 20, **kw}


def test_the_note_reaches_the_tailor(ran):
    stores = _Stores(_row())

    R.retailor("stripe#1", "lean harder on distributed systems", stores=stores)

    assert ran["state"]["tailor_note"] == "lean harder on distributed systems"


def test_the_note_is_stored_so_a_later_re_tailor_keeps_it(ran):
    stores = _Stores(_row())

    R.retailor("stripe#1", "lean harder on distributed systems", stores=stores)

    assert stores.tracking.row["tailor_note"] == "lean harder on distributed systems"


def test_a_stored_note_is_reused_when_none_is_given(ran):
    stores = _Stores(_row(tailor_note="mention Kubernetes"))

    R.retailor("stripe#1", stores=stores)

    assert ran["state"]["tailor_note"] == "mention Kubernetes"


def test_clearing_the_box_clears_the_note(ran):
    """The textarea shows the stored note, so what is in it IS the note. Sending
    it back empty is how the owner removes one."""
    stores = _Stores(_row(tailor_note="mention Kubernetes"))

    R.retailor("stripe#1", "", stores=stores)

    assert stores.tracking.row["tailor_note"] == ""
    assert ran["state"]["tailor_note"] == ""


@pytest.mark.parametrize("status", ["applied", "applied_manual"])
def test_an_applied_row_is_refused(ran, status):
    """The document went out. Rewriting the row would present a résumé the
    employer never received as the one that was sent."""
    stores = _Stores(_row(status=status))

    out = R.retailor("stripe#1", "lean into Kubernetes", stores=stores)

    assert out["result"] == "refused"
    assert not ran, "the tailor must not have run"
    assert "tailor_note" not in stores.tracking.row, "an applied row is not edited"


def test_a_row_mid_apply_is_refused(ran):
    """A browser is on the form right now; swapping the PDF under it is the kind
    of race that attaches one job's résumé to another's application."""
    stores = _Stores(_row(status="submitting"))

    out = R.retailor("stripe#1", "lean into Kubernetes", stores=stores)

    assert out["result"] == "refused"
    assert not ran


def test_an_unknown_row_is_reported_not_crashed(ran):
    out = R.retailor("nope#1", "x", stores=_Stores(_row()))

    assert out["result"] == "missing_row"
    assert not ran


def test_the_card_shows_as_tailoring_while_it_runs(monkeypatch):
    """A click that changes nothing on screen reads as a dead button — the same
    complaint the gate answer produced. The row moves to the tailoring view for
    the duration, which is a real pipeline status the board already renders."""
    seen = []

    async def _fake(pk, state, title, company):
        seen.append(stores.tracking.row["status"])

    monkeypatch.setattr(R, "_tailor_only", _fake)
    monkeypatch.setattr(R, "_base_latex", lambda: "seed")
    monkeypatch.setattr(R, "_github_context", lambda: "")
    monkeypatch.setattr(R, "_prefs_notes", lambda: "")
    stores = _Stores(_row(status="tailored"))

    R.retailor("stripe#1", "more Kubernetes", stores=stores)

    assert seen == ["tailoring"], "the card never showed it was working"


def test_the_row_gets_its_status_back_and_keeps_its_place(ran):
    """Re-tailoring is not a reset. A queued row that gets a note must not fall
    out of the queue or lose its approval — it only borrows the tailoring view."""
    stores = _Stores(_row(status="tailored"))

    R.retailor("stripe#1", "more Kubernetes", stores=stores)

    assert stores.tracking.row["status"] == "tailored"


def test_a_failed_re_tailor_still_hands_the_status_back(monkeypatch):
    """Left on `tailoring`, the row would have no Apply button and no way back —
    a transient failure would turn into a job the owner cannot recover."""
    async def _boom(pk, state, title, company):
        raise RuntimeError("the model fell over")

    monkeypatch.setattr(R, "_tailor_only", _boom)
    monkeypatch.setattr(R, "_base_latex", lambda: "seed")
    monkeypatch.setattr(R, "_github_context", lambda: "")
    monkeypatch.setattr(R, "_prefs_notes", lambda: "")
    stores = _Stores(_row(status="tailored"))

    out = R.retailor("stripe#1", "more Kubernetes", stores=stores)

    assert out["result"] == "error"
    assert stores.tracking.row["status"] == "tailored"


def test_the_pipeline_sees_the_same_note_as_the_button(monkeypatch):
    """The run that actually reaches the employer is the automatic one. If the
    note applied only on the button, the stale-résumé re-tailor would quietly
    send the résumé the owner had just corrected away."""
    monkeypatch.setattr(R, "_base_latex", lambda: "seed")
    monkeypatch.setattr(R, "_github_context", lambda: "")
    monkeypatch.setattr(R, "_prefs_notes", lambda: "")

    state = R._session_state(_row(tailor_note="mention Kubernetes"), "jd text")

    assert state["tailor_note"] == "mention Kubernetes"


def test_a_row_without_a_note_gets_an_empty_one_not_a_missing_key(monkeypatch):
    """ADK templates the instruction from state; a key that is absent is not the
    same as one that is blank."""
    monkeypatch.setattr(R, "_base_latex", lambda: "seed")
    monkeypatch.setattr(R, "_github_context", lambda: "")
    monkeypatch.setattr(R, "_prefs_notes", lambda: "")

    assert R._session_state(_row(), "jd text")["tailor_note"] == ""


def test_the_applier_will_not_dispatch_a_row_mid_tailor():
    """A browser uploading a PDF that is being rewritten under it is how one
    job's résumé ends up attached to another job's application.

    `retailor` refuses a row that is already `submitting`, which closes the race
    from the applier's side. This closes it from the other side: between the
    queue leasing a row and the applier claiming it, a re-tailor may have started.
    The result must be RETRYABLE — the row will be tailored again in a moment and
    should apply then, not be given up on.
    """
    import inspect

    from core.apply_queue import TERMINAL, is_retryable

    src = inspect.getsource(R._apply_direct)
    assert 'Status.TAILORING' in src, \
        "the applier does not check for a row that is being re-tailored"
    assert "mid_tailor" not in TERMINAL
    assert is_retryable({"result": "failed", "reason": "mid_tailor"})[0] is True


def test_a_re_tailor_is_stamped_so_the_list_can_show_it(ran):
    """`tailored_at` is written once, on the first move into tailored, and never
    again — so a re-tailored row looked identical to one nobody had touched. The
    only way to tell was the activity feed scrolling past.

    Stamped separately from `tailored_at` on purpose: this marks the ones the
    owner steered by hand, which is a different question from how old the résumé
    is, and an automatic rebuild must not claim it.
    """
    stores = _Stores(_row())

    R.retailor("stripe#1", "lead with Kubernetes", stores=stores)

    assert stores.tracking.row["retailored_at"], "nothing marks it as re-tailored"


def test_a_refused_row_is_never_stamped(ran):
    stores = _Stores(_row(status="applied"))

    R.retailor("stripe#1", "lead with Kubernetes", stores=stores)

    assert "retailored_at" not in stores.tracking.row


def test_a_failed_re_tailor_is_never_stamped(monkeypatch):
    """The stamp says the résumé was rebuilt. If it was not, it must not say so."""
    async def _boom(pk, state, title, company):
        raise RuntimeError("the model fell over")

    monkeypatch.setattr(R, "_tailor_only", _boom)
    monkeypatch.setattr(R, "_base_latex", lambda: "seed")
    monkeypatch.setattr(R, "_github_context", lambda: "")
    monkeypatch.setattr(R, "_prefs_notes", lambda: "")
    stores = _Stores(_row())

    R.retailor("stripe#1", "lead with Kubernetes", stores=stores)

    assert not stores.tracking.row.get("retailored_at")
