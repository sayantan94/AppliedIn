"""Applying again to a posting already applied to — under a different identity.

The applied row is history: it holds the confirmation the employer sent back
and must never be rewritten. So a re-application is a NEW row for the same
posting, keyed `<pk>~2`, stamped with the new profile, linked to the original
both ways, and sent through the ordinary gate like any other found job.

One rule stays in code. The identity has to differ from the one that already
applied: the same address on the same posting twice is precisely the duplicate
the guard exists to refuse, and a different profile is the only thing that
makes "again" mean something.
"""

from __future__ import annotations

import fakeredis
import pytest

from core import profiles as prof


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

    def put_new(self, job, *, status=None):
        if job.pk in self.rows:
            return False
        self.rows[job.pk] = {"pk": job.pk, "company": job.company, "title": job.title,
                             "jd_url": job.jd_url, "jd_text": job.jd_text, "status": "found"}
        return True


class _Stores:
    def __init__(self, rows):
        self.tracking = _Tracking(rows)


APPLIED = {"pk": "netflix#790315922591", "company": "Netflix", "title": "SWE L5",
           "jd_url": "https://x/1", "jd_text": "text " * 100, "status": "applied",
           "profile_id": "personal-2", "confirmation_id": "Thank you"}


@pytest.fixture
def world(monkeypatch, tmp_path):
    from core import flags

    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(flags, "_redis", lambda: fake)
    monkeypatch.setattr(prof, "_path", lambda: tmp_path / "profiles.yaml")
    prof.save([{"id": "personal-2", "label": "P2", "email": "a+2@x.com"},
               {"id": "personal-5", "label": "P5", "email": "a+5@x.com"},
               {"id": "rot", "label": "Rot", "email": "a@x.com", "kind": "rotating"}], "personal-2")
    return _Stores([dict(APPLIED)])


def test_a_new_row_is_created_and_linked_both_ways(world):
    out = prof.reapply("netflix#790315922591", prof.get("personal-5"), world)

    assert out["ok"] and out["pk"] == "netflix#790315922591~2"
    new = world.tracking.rows["netflix#790315922591~2"]
    assert new["status"] == "found"
    assert new["profile_id"] == "personal-5"
    assert new["reapplied_from"] == "netflix#790315922591"
    assert world.tracking.rows["netflix#790315922591"]["reapplied_as"] == ["netflix#790315922591~2"]


def test_the_applied_row_is_not_rewritten(world):
    prof.reapply("netflix#790315922591", prof.get("personal-5"), world)

    orig = world.tracking.rows["netflix#790315922591"]
    assert orig["status"] == "applied"
    assert orig["profile_id"] == "personal-2"
    assert orig["confirmation_id"] == "Thank you"


def test_the_same_identity_is_refused(world):
    out = prof.reapply("netflix#790315922591", prof.get("personal-2"), world)

    assert out["ok"] is False
    assert "same" in out["error"].lower()
    assert "netflix#790315922591~2" not in world.tracking.rows


def test_a_rotating_template_is_refused(world):
    assert prof.reapply("netflix#790315922591", prof.get("rot"), world)["ok"] is False


def test_only_an_applied_row_can_be_reapplied(world):
    world.tracking.rows["netflix#790315922591"]["status"] = "tailored"

    assert prof.reapply("netflix#790315922591", prof.get("personal-5"), world)["ok"] is False


def test_a_third_application_gets_its_own_row(world):
    prof.reapply("netflix#790315922591", prof.get("personal-5"), world)
    world.tracking.rows["netflix#790315922591~2"]["status"] = "applied"
    prof.save([{"id": "personal-2", "label": "P2", "email": "a+2@x.com"},
               {"id": "personal-5", "label": "P5", "email": "a+5@x.com"},
               {"id": "personal-6", "label": "P6", "email": "a+6@x.com"}], "personal-2")

    out = prof.reapply("netflix#790315922591", prof.get("personal-6"), world)

    assert out["pk"] == "netflix#790315922591~3"
    assert world.tracking.rows["netflix#790315922591"]["reapplied_as"] == [
        "netflix#790315922591~2", "netflix#790315922591~3"]


def test_an_identity_already_used_on_this_posting_is_refused(world):
    """Two rows for the posting, one per identity. A third try with an identity
    either of them already used is the duplicate, whichever row it targets."""
    prof.reapply("netflix#790315922591", prof.get("personal-5"), world)

    out = prof.reapply("netflix#790315922591", prof.get("personal-5"), world)

    assert out["ok"] is False
