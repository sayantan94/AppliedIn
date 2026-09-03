"""A profile chosen for a company is a standing rule, not a one-time stamp.

"Apply as personal-5" on Netflix has to mean every application at Netflix from
now on — including postings discovered next week that nobody has looked at —
not only the rows that happened to exist when the button was pressed. Rotation
already works that way: a persisted company → address binding, applied at
dispatch. This is the same shape for a profile the owner chose.

Precedence, most specific first: a profile set on the row itself, then the
company's binding, then the default. And the PDF has to follow: a row tailored
under the old identity that is dispatched under the binding is re-rendered at
dispatch, or the form and the résumé go out saying two different addresses.
"""

from __future__ import annotations

import inspect

import fakeredis
import pytest

from core import profiles as prof
from core.apply_queue import ApplyQueue


@pytest.fixture
def yaml_home(tmp_path, monkeypatch):
    from core import flags

    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(flags, "_redis", lambda: fake)
    monkeypatch.setattr(prof, "_path", lambda: tmp_path / "profiles.yaml")
    prof.save([{"id": "primary", "label": "Primary", "email": "a@x.com"},
               {"id": "personal-5", "label": "Personal 5", "email": "a+5@x.com"}], "primary")
    return tmp_path


def test_bind_persists_and_reads_back(yaml_home):
    prof.bind("Netflix", "personal-5")

    assert prof.binding("netflix") == "personal-5"
    assert prof.binding("NETFLIX ") == "personal-5"
    assert prof.bindings() == {"netflix": "personal-5"}


def test_unbind(yaml_home):
    prof.bind("Netflix", "personal-5")
    prof.unbind("Netflix")

    assert prof.binding("netflix") == ""


def test_the_rule_is_the_company_page_value(yaml_home):
    """One value, one place: the company page's "Apply as" preference is the
    binding. Discovery has always read it; now tailoring and dispatch do too."""
    from core import flags

    prof.bind("Netflix", "personal-5")

    assert flags.company_pref("netflix").get("profile_id") == "personal-5"


def test_a_rule_pointing_at_a_removed_profile_falls_back_to_default(yaml_home):
    prof.bind("Netflix", "personal-5")
    prof.save([{"id": "primary", "label": "Primary", "email": "a@x.com"}], "primary")

    assert prof.resolve_for({"company": "Netflix", "profile_id": ""}).id == "primary"


def test_bind_refuses_an_unknown_profile(yaml_home):
    with pytest.raises(ValueError):
        prof.bind("Netflix", "nope")


def test_resolve_for_precedence(yaml_home):
    prof.bind("Netflix", "personal-5")

    assert prof.resolve_for({"company": "Netflix", "profile_id": ""}).id == "personal-5"
    assert prof.resolve_for({"company": "Netflix", "profile_id": "primary"}).id == "primary"
    assert prof.resolve_for({"company": "Stripe", "profile_id": ""}).id == "primary"


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


def test_assign_company_sets_the_standing_rule(yaml_home, monkeypatch):
    monkeypatch.setattr(prof, "retarget", lambda *a, **k: True)
    stores = _Stores([{"pk": "netflix#1", "company": "Netflix", "status": "tailored",
                       "profile_id": "", "resume_tex_key": "r"}])

    prof.assign_company("Netflix", prof.get("personal-5"), stores, ApplyQueue(stores.tracking.r))

    assert prof.binding("netflix") == "personal-5"


def test_assign_company_refuses_a_rotating_company(yaml_home, monkeypatch):
    """Two standing rules for one company would fight at dispatch. Rotation is
    the one already in force; retire it first."""
    from core import rotation

    monkeypatch.setattr(rotation, "binding", lambda co: {"profile": "rot"} if co.lower() == "netflix" else None)
    stores = _Stores([])

    out = prof.assign_company("Netflix", prof.get("personal-5"), stores, ApplyQueue(stores.tracking.r))

    assert out["ok"] is False
    assert "rotat" in out["error"].lower()
    assert prof.binding("netflix") == ""


def test_dispatch_and_tailor_resolve_through_the_binding():
    """Pinned by reading the source: every place that turns a row into a profile
    goes through resolve_for, and dispatch re-renders when the binding differs
    from what the row was tailored under."""
    from agent import graph, run

    assert "resolve_for(" in inspect.getsource(run._apply_direct)
    src = inspect.getsource(run._apply_direct)
    i = src.index("resolve_for(")
    assert "retarget(" in src[i:i + 1400], "dispatch must re-render the PDF to match the binding"
    assert inspect.getsource(graph.save_tailored_resume).count("resolve_for(") >= 1
    assert "resolve((stores.tracking.get(pk) or {}).get(\"profile_id\"" not in inspect.getsource(graph)


def test_assign_reports_the_rows_still_to_tailor(yaml_home, monkeypatch):
    """"Apply as X" on a company full of found postings has to end with something
    to press Apply on, so the rows with no résumé are handed back for tailoring."""
    monkeypatch.setattr(prof, "retarget", lambda *a, **k: True)
    stores = _Stores([
        {"pk": "netflix#1", "company": "Netflix", "status": "found", "profile_id": "", "resume_tex_key": ""},
        {"pk": "netflix#2", "company": "Netflix", "status": "tailored", "profile_id": "", "resume_tex_key": "r"},
    ])

    out = prof.assign_company("Netflix", prof.get("personal-5"), stores, ApplyQueue(stores.tracking.r))

    assert out["untailored"] == ["netflix#1"]
    assert stores.tracking.rows["netflix#1"]["profile_id"] == "personal-5"


def test_the_company_page_and_the_queue_dropdown_are_one_door():
    """Pinned by reading the source: the prefs endpoint hands a profile change to
    the same helper the queue dropdown uses, and that helper tailors what has no
    résumé yet."""
    import inspect

    import server

    src = inspect.getsource(server.create_app) if hasattr(server, "create_app") else open(server.__file__).read()
    assert "_set_company_profile(name, wanted, background)" in src
    assert "background.add_task(_tailor_and_queue, company, untailored, stores, q)" in src


def test_choosing_a_profile_on_the_company_page_keeps_its_other_rules():
    """A save carrying only `profile_id` must not become `set_company_pref(name, {})`,
    which means reset-to-global and would wipe titles and seniority as a side
    effect of choosing who applies."""
    import inspect

    import server

    src = open(server.__file__).read()
    i = src.index('over.pop("profile_id", "")')
    window = src[i:i + 900]
    assert "if over or not profile_change:" in window
    assert "flags.set_company_pref(name, over)" in window


def test_dispatch_renders_the_pdf_for_the_sending_profile_every_time():
    """Not only when the stamp changed. A board-wide re-render was cut off by a
    restart at 141 of 378 rows, leaving stamps whose PDFs did not match."""
    import inspect

    from agent import run

    src = inspect.getsource(run._apply_direct)
    i = src.index("resolve_for(")
    seg = src[i:i + 1400]
    assert 'if _prof and _row.get("resume_tex_key"):' in seg
    j = seg.index("_profiles.retarget(pk, _prof, stores)")
    assert seg[:j].count("if _prof.id !=") == 1
    assert seg[j - 120:j].count("if _prof.id !=") == 0, "retarget must not be inside the changed-stamp branch"
