"""Rotating profiles: one address per N applications at a company.

These pin the two things that make the feature safe rather than merely clever.
The first is that a word, once minted, is gone forever — reusing one would put
two applications on the same address at different employers and quietly break the
count that is the whole point. The second is that the address is settled at
DISPATCH and the résumé re-rendered to match, so the PDF and the form can never
disagree — and an address is spent only by an application that actually went
out, never by one that was merely prepared.

Offline by design, like the rest of the suite: a stub tracking store stands in
for Dynamo/Redis, and nothing here touches a browser or the network.
"""

from __future__ import annotations

import pytest


class _Tracking:
    """Just enough tracking store to count rows and stamp a profile."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = {r["pk"]: dict(r) for r in (rows or [])}

    def all(self) -> list[dict]:
        return list(self.rows.values())

    def get(self, pk: str) -> dict | None:
        row = self.rows.get(pk)
        return dict(row) if row else None

    def set_status(self, pk: str, status, **fields) -> None:  # noqa: ANN001
        row = self.rows.setdefault(pk, {"pk": pk})
        row["status"] = getattr(status, "value", status)
        row.update(fields)


class _Stores:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.tracking = _Tracking(rows)


@pytest.fixture
def local(tmp_path, monkeypatch):
    """Point the profile + rotation ledgers at a scratch directory."""
    monkeypatch.setenv("APPLIEDIN_LOCAL_DIR", str(tmp_path))
    from core.config import get_settings

    try:
        get_settings.cache_clear()
    except AttributeError:
        pass
    yield tmp_path
    try:
        get_settings.cache_clear()
    except AttributeError:
        pass


@pytest.fixture
def rotating(local):
    """A rotating profile named `rot`, plus the module under test."""
    from core import profiles, rotation

    profiles.save([{"id": "rot", "label": "Rotating", "kind": "rotating",
                    "email": "owner@gmail.com", "phone": "+1 555 010 0123",
                    "limit": 5}])
    return rotation


def _rows(alias_id: str, n: int, status: str = "applied") -> list[dict]:
    return [{"pk": f"openai#{i}", "company": "OpenAI", "status": status,
             "profile_id": alias_id} for i in range(n)]


# --- minting ---------------------------------------------------------------

def test_alias_carries_the_base_phone_and_a_suffixed_email(rotating):
    rotating.bind("OpenAI", "rot")
    alias = rotating.assign("openai#1", "OpenAI", _Stores())
    assert alias.email.startswith("owner+")
    assert alias.email.endswith("@gmail.com")
    # The person is the same person: only the address string moves.
    assert alias.phone == "+1 555 010 0123"


def test_a_word_is_never_offered_twice(rotating):
    """Across companies, not just within one — an address must be unique."""
    rotating.bind("OpenAI", "rot", limit=1)
    rotating.bind("Ramp", "rot", limit=1)
    seen = set()
    stores = _Stores()
    for i in range(12):
        company = "OpenAI" if i % 2 else "Ramp"
        alias = rotating.assign(f"{company}#{i}", company, stores)
        assert alias.email not in seen, "a burned word came back"
        seen.add(alias.email)
        # Each new row fills its alias, so the next assignment must mint.
        stores.tracking.set_status(f"{company}#{i}", "applied", profile_id=alias.id)


def test_rotation_fires_at_the_limit_and_not_before(rotating):
    rotating.bind("OpenAI", "rot", limit=5)
    stores = _Stores(_rows("", 0))
    first = rotating.assign("openai#0", "OpenAI", stores)
    stores.tracking.rows = {r["pk"]: r for r in _rows(first.id, 4)}
    # Four used: the fifth application still belongs to the same address.
    assert rotating.assign("openai#4", "OpenAI", stores).id == first.id
    stores.tracking.rows = {r["pk"]: r for r in _rows(first.id, 5)}
    assert rotating.assign("openai#5", "OpenAI", stores).id != first.id


def test_per_company_limit_overrides_the_profile_default(rotating):
    """Ramp allows two, not five — the binding's number is the one that counts."""
    rotating.bind("Ramp", "rot", limit=2)
    stores = _Stores()
    first = rotating.assign("ramp#0", "Ramp", stores)
    stores.tracking.rows = {r["pk"]: r for r in _rows(first.id, 2)}
    assert rotating.assign("ramp#2", "Ramp", stores).id != first.id


def test_a_tag_already_used_by_a_profile_is_never_minted(rotating, monkeypatch):
    """Someone who has been rotating by hand already has +job in profiles.yaml.
    Minting it again would put a second employer's applications on an address the
    first one is already counting — the exact collision this feature prevents."""
    from core import profiles, rotation

    profiles.save([{"id": "rot", "label": "Rotating", "kind": "rotating",
                    "email": "owner@gmail.com", "phone": "+1 555 010 0123"},
                   {"id": "old", "label": "Old hunt",
                    "email": "owner+job@gmail.com"}])
    monkeypatch.setattr(rotation, "_pool", lambda: ["job"])
    rotating.bind("OpenAI", "rot")
    # "job" is the only word in the pool and it is already spoken for, so the
    # fallback must produce something else rather than reuse it.
    assert rotating.assign("openai#1", "OpenAI", _Stores()).email \
        != "owner+job@gmail.com"


# --- counting --------------------------------------------------------------

def test_an_uncertain_submit_spends_its_slot(rotating):
    """`uncertain` is a FAILED row whose page may already have taken the
    application. "May have applied" has to read as "has"."""
    rotating.bind("OpenAI", "rot", limit=2)
    stores = _Stores()
    first = rotating.assign("openai#0", "OpenAI", stores)
    stores.tracking.rows = {
        "a": {"pk": "a", "status": "failed", "fail_kind": "uncertain",
              "profile_id": first.id},
        "b": {"pk": "b", "status": "applied", "profile_id": first.id},
    }
    assert rotating.assign("openai#2", "OpenAI", stores).id != first.id


def test_only_a_submission_spends_a_slot(rotating):
    """The rule that was wrong the other way round. Five OpenAI jobs were
    dispatched, gated on a missing résumé and came back having submitted nothing
    — and the address read 5/5 and was retired unused. A dispatch, a gate, a
    tailored row waiting its turn: none of them is an application."""
    rotating.bind("OpenAI", "rot", limit=2)
    stores = _Stores()
    first = rotating.assign("openai#0", "OpenAI", stores)
    stores.tracking.rows = {
        "a": {"pk": "a", "status": "tailored", "profile_id": first.id},
        "b": {"pk": "b", "status": "needs_human", "profile_id": first.id},
        "c": {"pk": "c", "status": "skipped", "profile_id": first.id},
        "d": {"pk": "d", "status": "failed", "fail_kind": "application_limit",
              "profile_id": first.id},
    }
    assert rotating.used(first.id, stores) == 0
    assert rotating.assign("openai#9", "OpenAI", stores).id == first.id


def test_the_address_rotates_once_two_applications_have_gone_out(rotating):
    rotating.bind("OpenAI", "rot", limit=2)
    stores = _Stores()
    first = rotating.assign("openai#0", "OpenAI", stores)
    stores.tracking.rows = {
        "a": {"pk": "a", "status": "applied", "profile_id": first.id},
        "b": {"pk": "b", "status": "applied_manual", "profile_id": first.id},
    }
    assert rotating.assign("openai#2", "OpenAI", stores).id != first.id


# --- assignment ------------------------------------------------------------

def test_assignment_stamps_the_row_so_the_pdf_and_the_form_agree(rotating):
    from core import profiles

    rotating.bind("OpenAI", "rot")
    stores = _Stores([{"pk": "openai#1", "company": "OpenAI", "status": "found"}])
    alias = rotating.assign("openai#1", "OpenAI", stores)
    stamped = stores.tracking.get("openai#1")["profile_id"]
    assert stamped == alias.id
    # Both readers of that field must resolve to the SAME address: the résumé's
    # contact line (apply_to_latex) and the form's answers (override).
    resolved = profiles.resolve(stamped)
    assert resolved.email == alias.email
    tex = r"\href{mailto:old@example.com}{old@example.com}"
    assert alias.email in profiles.apply_to_latex(tex, resolved)
    assert profiles.resolve(stamped).override({"Email": "old@example.com"})["Email"] \
        == alias.email


def test_a_hand_chosen_profile_survives_assignment(rotating):
    """An explicit choice by the owner outranks the rule."""
    from core import profiles

    profiles.save([{"id": "rot", "label": "Rotating", "kind": "rotating",
                    "email": "owner@gmail.com", "phone": "+1 555 010 0123"},
                   {"id": "personal", "label": "Personal",
                    "email": "other@fastmail.com"}])
    rotating.bind("OpenAI", "rot")
    stores = _Stores([{"pk": "openai#1", "company": "OpenAI", "status": "found",
                       "profile_id": "personal"}])
    assert rotating.assign("openai#1", "OpenAI", stores) is None
    assert stores.tracking.get("openai#1")["profile_id"] == "personal"


def test_a_retry_keeps_the_address_it_already_had(rotating):
    """Re-running a job must not burn a second slot for the same application."""
    rotating.bind("OpenAI", "rot")
    stores = _Stores([{"pk": "openai#1", "company": "OpenAI", "status": "found"}])
    first = rotating.assign("openai#1", "OpenAI", stores)
    again = rotating.assign("openai#1", "OpenAI", stores)
    assert again.id == first.id


def test_an_unbound_company_is_untouched(rotating):
    stores = _Stores([{"pk": "ramp#1", "company": "Ramp", "status": "found"}])
    assert rotating.assign("ramp#1", "Ramp", stores) is None
    assert stores.tracking.get("ramp#1").get("profile_id") in (None, "")


# --- the last check, at apply time -----------------------------------------

def test_a_job_tailored_before_rotation_still_applies_under_the_alias(rotating, monkeypatch):
    """The gap `assign()` cannot close: this row was stamped last week, and the
    company only started rotating today. Without the apply-time check it would go
    out under the very address rotation exists to retire."""
    from core import profiles

    seen = {}
    monkeypatch.setattr(profiles, "retarget",
                        lambda pk, prof, st=None: seen.update({pk: prof.email}) or True)
    rotating.bind("OpenAI", "rot")
    stores = _Stores([{"pk": "openai#1", "company": "OpenAI", "status": "tailored",
                       "profile_id": "personal"}])
    alias = rotating.ensure("openai#1", "OpenAI", stores)
    assert alias.email.startswith("owner+")
    assert stores.tracking.get("openai#1")["profile_id"] == alias.id
    # The PDF has to follow, or the form and the résumé disagree.
    assert seen["openai#1"] == alias.email


def test_the_apply_time_check_does_not_burn_a_second_slot(rotating, monkeypatch):
    """Called on every apply, including retries of the same job."""
    from core import profiles

    monkeypatch.setattr(profiles, "retarget", lambda *a, **k: True)
    rotating.bind("OpenAI", "rot", limit=5)
    stores = _Stores([{"pk": "openai#1", "company": "OpenAI", "status": "tailored"}])
    first = rotating.ensure("openai#1", "OpenAI", stores)
    assert rotating.ensure("openai#1", "OpenAI", stores).id == first.id
    assert rotating.used(first.id, stores) == 0, "dispatched twice, submitted never"
    stores.tracking.set_status("openai#1", "applied", profile_id=first.id)
    assert rotating.used(first.id, stores) == 1


def test_the_apply_time_check_is_inert_for_an_unbound_company(rotating):
    stores = _Stores([{"pk": "ramp#1", "company": "Ramp", "status": "tailored",
                       "profile_id": "personal"}])
    assert rotating.ensure("ramp#1", "Ramp", stores) is None
    assert stores.tracking.get("ramp#1")["profile_id"] == "personal"


# --- backfill --------------------------------------------------------------

def test_backfill_moves_unsent_jobs_onto_the_rotating_address(rotating, monkeypatch):
    """Turning rotation on only changes what happens next; the board is already
    full of tailored jobs still pointing at the original address."""
    from core import profiles

    monkeypatch.setattr(profiles, "retarget", lambda *a, **k: True)  # no LaTeX here
    rotating.bind("OpenAI", "rot", limit=5)
    stores = _Stores([
        {"pk": "openai#1", "company": "OpenAI", "status": "tailored", "profile_id": "personal"},
        {"pk": "openai#2", "company": "OpenAI", "status": "needs_human"},
        {"pk": "openai#3", "company": "OpenAI", "status": "applied", "profile_id": "personal"},
        {"pk": "ramp#1", "company": "Ramp", "status": "tailored"},
    ])
    out = rotating.backfill("OpenAI", stores)
    assert out["moved"] == 2
    rows = {r["pk"]: r for r in stores.tracking.all()}
    assert rows["openai#1"]["profile_id"] == rows["openai#2"]["profile_id"]
    # Already sent: rewriting what it went out under would be a lie.
    assert rows["openai#3"]["profile_id"] == "personal"
    assert rows["ramp#1"].get("profile_id") in (None, "")


def test_backfill_spends_its_slots_on_what_is_closest_to_going_out(rotating, monkeypatch):
    """Five slots and hundreds of rows: an untailored posting taking one costs a
    whole tailoring run before anything is sent, while a finished résumé waits."""
    from core import profiles

    monkeypatch.setattr(profiles, "retarget", lambda *a, **k: True)
    rotating.bind("OpenAI", "rot", limit=2)
    stores = _Stores([
        {"pk": "found-1", "company": "OpenAI", "status": "found"},
        {"pk": "found-2", "company": "OpenAI", "status": "found"},
        {"pk": "ready-1", "company": "OpenAI", "status": "tailored"},
        {"pk": "gated-1", "company": "OpenAI", "status": "needs_human",
         "gate_reason": "approval"},
    ])
    out = rotating.backfill("OpenAI", stores)
    assert sorted(out["pks"]) == ["gated-1", "ready-1"]


def test_backfill_takes_every_job_off_the_old_identity(rotating, monkeypatch):
    """Past what the address can carry, a job must still not keep the identity
    being retired — it just gets its new one at dispatch instead of now."""
    from core import profiles

    monkeypatch.setattr(profiles, "retarget", lambda *a, **k: True)
    rotating.bind("OpenAI", "rot", limit=2)
    stores = _Stores([{"pk": f"openai#{i}", "company": "OpenAI", "status": "tailored",
                       "profile_id": "personal"} for i in range(6)])
    out = rotating.backfill("OpenAI", stores)
    assert (out["moved"], out["cleared"]) == (2, 4)
    assert not any(r["profile_id"] == "personal" for r in stores.tracking.all())
    # And nothing was minted for the four: one address so far, not three.
    assert len(rotating.aliases("OpenAI")) == 1


def test_backfill_never_mints_past_the_limit(rotating, monkeypatch):
    """Stamping every tailored job would burn one address per job — hundreds of
    them — on applications that may never be sent."""
    from core import profiles

    monkeypatch.setattr(profiles, "retarget", lambda *a, **k: True)
    rotating.bind("OpenAI", "rot", limit=2)
    stores = _Stores([{"pk": f"openai#{i}", "company": "OpenAI", "status": "tailored"}
                      for i in range(9)])
    out = rotating.backfill("OpenAI", stores)
    assert (out["moved"], out["left"]) == (2, 7)
    stamped = {r.get("profile_id") for r in stores.tracking.all() if r.get("profile_id")}
    assert len(stamped) == 1, "one address, not one per job"


# --- rotate & approve ------------------------------------------------------
# The one path that queues real applications with no further approval, so the
# rules it must not break are worth stating as tests rather than as comments.

@pytest.fixture
def queue():
    import fakeredis

    from core.apply_queue import ApplyQueue

    return ApplyQueue(fakeredis.FakeRedis(decode_responses=True))


def test_rotate_and_approve_requeues_under_the_new_address(rotating, queue, monkeypatch):
    """A job already in the queue was queued under the OLD address, and the queue
    item is what gets dispatched. It has to come out and go back in, or the very
    application this was meant to fix goes out under the address being retired."""
    from core import profiles

    monkeypatch.setattr(profiles, "retarget", lambda *a, **k: True)
    rotating.bind("OpenAI", "rot", limit=5)
    stores = _Stores([{"pk": "openai#1", "company": "OpenAI", "status": "tailored",
                       "profile_id": "personal"}])
    queue.put("openai#1", "OpenAI")
    out = rotating.rotate_and_approve("OpenAI", stores, queue)
    assert (out["dequeued"], out["queued"]) == (1, 1)
    assert [i["pk"] for i in queue.pending()] == ["openai#1"]
    assert stores.tracking.get("openai#1")["profile_id"] == out["alias"]


def test_rotate_and_approve_queues_everything_but_stamps_only_what_fits(
        rotating, queue, monkeypatch):
    """The limit bounds how many applications ONE ADDRESS carries, not how much
    work may be queued. Every tailored job goes in; the address for each is
    settled as it is dispatched, so an address is spent only when something is
    actually sent."""
    from core import profiles

    monkeypatch.setattr(profiles, "retarget", lambda *a, **k: True)
    rotating.bind("OpenAI", "rot", limit=5)
    stores = _Stores([{"pk": f"openai#{i}", "company": "OpenAI", "status": "tailored"}
                      for i in range(40)])
    out = rotating.rotate_and_approve("OpenAI", stores, queue)
    assert (out["queued"], len(queue.pending())) == (40, 40)
    assert out["moved"] == 5, "only five carry the address up front"
    # The other 35 are not left on the old identity either: each is settled as it
    # reaches the form. The address only moves on once applications have actually
    # gone out under it — five dispatches that submitted nothing do not retire it.
    assert rotating.ensure("openai#39", "OpenAI", stores).email == out["email"]
    for i in range(5):
        stores.tracking.set_status(f"openai#{i}", "applied", profile_id=out["alias"])
    assert rotating.ensure("openai#38", "OpenAI", stores).email != out["email"]


def test_rotate_and_approve_leaves_a_real_question_alone(rotating, queue, monkeypatch):
    """'No questions asked' means not asking again for an approval this press
    already gave — not answering a portal exercise nobody has answered."""
    from core import profiles

    monkeypatch.setattr(profiles, "retarget", lambda *a, **k: True)
    rotating.bind("OpenAI", "rot", limit=5)
    stores = _Stores([
        {"pk": "openai#1", "company": "OpenAI", "status": "needs_human",
         "gate_reason": "unknown_field",
         "gate_pending": {"question": "Complete the take-home exercise first"}},
        {"pk": "openai#2", "company": "OpenAI", "status": "needs_human",
         "gate_reason": "approval", "gate_pending": {"question": "Ready to apply?"}},
    ])
    out = rotating.rotate_and_approve("OpenAI", stores, queue)
    assert (out["queued"], out["blocked"]) == (1, 1)
    assert [i["pk"] for i in queue.pending()] == ["openai#2"]


def test_rotate_and_approve_never_touches_a_sent_application(rotating, queue, monkeypatch):
    """An UNCERTAIN submit is `failed` with fail_kind uncertain: the page may
    already have taken it, under whatever address it carried then. Re-pointing it
    would rewrite what an application that may exist went out under."""
    from core import profiles

    monkeypatch.setattr(profiles, "retarget", lambda *a, **k: True)
    rotating.bind("OpenAI", "rot", limit=5)
    stores = _Stores([
        {"pk": "openai#1", "company": "OpenAI", "status": "applied", "profile_id": "personal"},
        {"pk": "openai#2", "company": "OpenAI", "status": "failed",
         "fail_kind": "uncertain", "profile_id": "personal"},
    ])
    out = rotating.rotate_and_approve("OpenAI", stores, queue)
    assert (out["moved"], out["queued"]) == (0, 0)
    assert {r["profile_id"] for r in stores.tracking.all()} == {"personal"}


def test_rotate_and_approve_refuses_a_company_that_does_not_rotate(rotating, queue):
    stores = _Stores([{"pk": "ramp#1", "company": "Ramp", "status": "tailored"}])
    queue.put("ramp#1", "Ramp")
    out = rotating.rotate_and_approve("Ramp", stores, queue)
    assert out["ok"] is False
    assert len(queue.pending()) == 1, "a refusal must not empty the queue"


# --- styles ----------------------------------------------------------------

def test_dot_style_produces_gmail_equivalent_but_distinct_addresses(rotating):
    """Gmail ignores dots, so every placement reaches the same inbox — and no
    ATS normalises them, which is why this style exists at all."""
    rotating.bind("OpenAI", "rot", limit=1, style="dot")
    stores = _Stores()
    seen = set()
    for i in range(6):
        alias = rotating.assign(f"openai#{i}", "OpenAI", stores)
        local = alias.email.split("@")[0]
        assert "+" not in local
        assert local.replace(".", "") == "owner"
        assert not local.startswith(".") and not local.endswith(".")
        assert ".." not in local
        assert alias.email not in seen
        seen.add(alias.email)
        stores.tracking.set_status(f"openai#{i}", "applied", profile_id=alias.id)


def test_dot_style_is_refused_for_a_non_gmail_base(local):
    """Only Gmail ignores dots. Anywhere else this would be a different mailbox
    — an address that silently drops every recruiter reply."""
    from core import profiles, rotation

    profiles.save([{"id": "rot", "label": "Rotating", "kind": "rotating",
                    "email": "me@fastmail.com", "phone": "+1 555 010 0123"}])
    with pytest.raises(ValueError):
        rotation.bind("OpenAI", "rot", style="dot")


# --- resilience ------------------------------------------------------------

def test_a_broken_ledger_yields_no_rotation_rather_than_an_exception(rotating, local):
    """A file nobody can parse must never be able to stop an application."""
    rotating.bind("OpenAI", "rot")
    (local / "rotation.yaml").write_text("companies: [this is not: a mapping\n")
    stores = _Stores([{"pk": "openai#1", "company": "OpenAI", "status": "found"}])
    assert rotating.assign("openai#1", "OpenAI", stores) is None


def test_a_rotating_template_is_never_returned_as_the_profile_to_apply_under(rotating):
    """Its base address is the one thing that must never reach a form: every
    application under it is a slot nothing counted."""
    from core import profiles

    assert profiles.resolve("") is None
    assert profiles.resolve("rot") is None


def test_binding_is_case_and_space_insensitive(rotating):
    rotating.bind("  OpenAI ", "rot")
    stores = _Stores()
    assert rotating.assign("openai#1", "openai", stores) is not None


def test_unbind_stops_minting_and_keeps_the_history(rotating):
    rotating.bind("OpenAI", "rot")
    stores = _Stores()
    alias = rotating.assign("openai#1", "OpenAI", stores)
    rotating.unbind("OpenAI")
    assert rotating.assign("openai#2", "OpenAI", _Stores()) is None
    # The ledger is the record of which address received which employer's mail,
    # so an alias is retired, never deleted.
    from core import profiles

    assert profiles.resolve(alias.id).email == alias.email
