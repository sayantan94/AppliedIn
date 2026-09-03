"""Choose the identity a whole company's applications go out under.

"Apply profile to all" was board-wide, and the per-job picker was one drawer at
a time. Neither answers the question the owner actually has — "send everything
at Netflix as personal-5" — and the rotation button answers it only for
companies that rotate. This is the same press, for a profile the owner picked.

The rules that make it safe are the ones rotation already follows:
  * anything already sent is history: applied rows keep their identity, and are
    counted so the owner sees they were left alone;
  * a job sitting in the apply queue was queued under the OLD profile and the
    queue item is what dispatches — so it comes out and goes back in;
  * a failed or errored job is revived under the new profile — that is the
    "apply again" — but only if it has a résumé; otherwise it goes back to
    `found` so Process tailors one.
"""

from __future__ import annotations

import fakeredis
import pytest

from core import profiles as prof
from core.apply_queue import ApplyQueue

P5 = prof.Profile(id="personal-5", label="Personal 5", email="me+5@x.com", phone="")


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


class _Stores:
    def __init__(self, rows):
        self.tracking = _Tracking(rows)


def _rows():
    return [
        {"pk": "netflix#1", "company": "Netflix", "status": "tailored", "profile_id": "personal-4", "resume_tex_key": "resumes/netflix#1.tex"},
        {"pk": "netflix#2", "company": "Netflix", "status": "tailored", "profile_id": "personal-4", "resume_tex_key": "resumes/netflix#2.tex"},
        {"pk": "netflix#3", "company": "Netflix", "status": "applied", "profile_id": "personal-4", "resume_tex_key": "resumes/netflix#3.tex"},
        {"pk": "netflix#4", "company": "Netflix", "status": "failed", "profile_id": "personal-4", "resume_tex_key": "resumes/netflix#4.tex"},
        {"pk": "netflix#5", "company": "Netflix", "status": "error", "profile_id": "", "resume_tex_key": ""},
        {"pk": "netflix#6", "company": "Netflix", "status": "found", "profile_id": "", "resume_tex_key": ""},
        {"pk": "netflix#7", "company": "Netflix", "status": "submitting", "profile_id": "personal-4", "resume_tex_key": "resumes/netflix#7.tex"},
        {"pk": "stripe#1", "company": "Stripe", "status": "tailored", "profile_id": "personal-4", "resume_tex_key": "resumes/stripe#1.tex"},
    ]


@pytest.fixture
def world(monkeypatch, tmp_path):
    from core import flags, rotation

    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(flags, "_redis", lambda: fake)
    monkeypatch.setattr(prof, "_path", lambda: tmp_path / "profiles.yaml")
    prof.save([{"id": "personal-4", "label": "Personal 4", "email": "a+4@x.com"},
               {"id": "personal-5", "label": "Personal 5", "email": "me+5@x.com"}], "personal-4")
    monkeypatch.setattr(rotation, "binding", lambda co: None)
    stores = _Stores(_rows())
    q = ApplyQueue(stores.tracking.r)
    q.put("netflix#1", "Netflix")
    q.put("stripe#1", "Stripe")
    rendered = []
    monkeypatch.setattr(prof, "retarget", lambda pk, p, s=None: rendered.append(pk) or True)
    return stores, q, rendered


def test_unsent_rows_are_repointed_and_rerendered(world):
    stores, q, rendered = world

    out = prof.assign_company("Netflix", P5, stores, q)

    for pk in ("netflix#1", "netflix#2", "netflix#4", "netflix#6"):
        assert stores.tracking.rows[pk]["profile_id"] == "personal-5", pk
    assert set(rendered) == {"netflix#1", "netflix#2", "netflix#4"}
    assert out["repointed"] == 5


def test_sent_and_in_flight_rows_are_left_alone(world):
    stores, q, _ = world

    out = prof.assign_company("Netflix", P5, stores, q)

    assert stores.tracking.rows["netflix#3"]["profile_id"] == "personal-4"
    assert stores.tracking.rows["netflix#7"]["profile_id"] == "personal-4"
    assert out["left_alone"] == 2


def test_other_companies_are_untouched(world):
    stores, q, _ = world

    prof.assign_company("Netflix", P5, stores, q)

    assert stores.tracking.rows["stripe#1"]["profile_id"] == "personal-4"
    assert any(i["pk"] == "stripe#1" for i in q.pending())


def test_queued_rows_come_out_and_go_back_in(world):
    """The queue item is what dispatches, and it was queued under the old
    identity — the exact mistake rotation exists to prevent."""
    stores, q, _ = world

    out = prof.assign_company("Netflix", P5, stores, q)

    pks = [i["pk"] for i in q.pending()]
    assert pks.count("netflix#1") == 1
    assert out["dequeued"] == 1


def test_failed_with_a_resume_is_revived_and_queued(world):
    stores, q, _ = world

    prof.assign_company("Netflix", P5, stores, q)

    assert stores.tracking.rows["netflix#4"]["status"] == "tailored"
    assert "netflix#4" in [i["pk"] for i in q.pending()]


def test_errored_without_a_resume_goes_back_to_found(world):
    stores, q, _ = world

    prof.assign_company("Netflix", P5, stores, q)

    assert stores.tracking.rows["netflix#5"]["status"] == "found"
    assert "netflix#5" not in [i["pk"] for i in q.pending()]
    assert stores.tracking.rows["netflix#5"]["profile_id"] == "personal-5"


def test_everything_tailored_ends_up_queued(world):
    stores, q, _ = world

    out = prof.assign_company("Netflix", P5, stores, q)

    assert set(i["pk"] for i in q.pending()) >= {"netflix#1", "netflix#2", "netflix#4"}
    assert out["queued"] == 3


def test_usage_is_derived_from_the_rows():
    stores = _Stores(_rows())

    u = prof.usage(stores)

    assert u["personal-4"]["applied"] == 1
    assert u["personal-4"]["unsent"] == 4
    assert u["personal-4"]["companies"]["Netflix"]["applied"] == 1
    assert "personal-5" not in u
