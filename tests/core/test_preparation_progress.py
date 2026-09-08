from core import preparation


def test_progress_counts_completed_work_separately_from_ready_resumes(monkeypatch):
    monkeypatch.setattr(preparation, "_BATCHES", {})
    with preparation.track([{"company": "Acme"}] * 3) as batch:
        batch.complete("tailored")
        batch.complete("skipped")
        batch.complete("error")
        state = preparation.snapshot()[0]
        counts = tuple(state[k] for k in ("completed", "prepared", "skipped", "failed"))
        assert counts == (3, 1, 1, 1)
    assert preparation.snapshot()[0]["stage"] == "Finished"


def test_interrupted_preparation_never_claims_all_jobs_finished(monkeypatch):
    monkeypatch.setattr(preparation, "_BATCHES", {})
    with preparation.track([{}, {}]) as batch:
        batch.complete("tailored")
    state = preparation.snapshot()[0]
    assert not state["active"]
    assert state["stage"] == "Stopped"
    assert state["completed"] == 1
