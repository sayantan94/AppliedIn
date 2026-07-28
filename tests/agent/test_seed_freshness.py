"""The tailored résumé must be built from TODAY's base.tex.

An ADK session stores the seed résumé when it is first created, and that same copy
is what the truthfulness check validates against. So a row first seen before an
edit to base.tex tailors from the old résumé AND is checked against the old
résumé: a project added to base.tex silently never reaches an employer, and
nothing objects.
"""

from types import SimpleNamespace

from agent.run import _stale_seed

BASE = "\\resumeSubheadingSingle{AppliedIn}{gh}\n\\resumeItem{one}"
OLD = "\\resumeItem{one}"


def test_a_session_seeded_from_an_older_resume_is_stale():
    assert _stale_seed(SimpleNamespace(state={"base_latex": OLD}), BASE)


def test_a_session_seeded_from_the_current_resume_is_not():
    assert not _stale_seed(SimpleNamespace(state={"base_latex": BASE}), BASE)


def test_a_session_with_no_seed_is_stale():
    """It cannot have been seeded from the current résumé, so it is rebuilt."""
    assert _stale_seed(SimpleNamespace(state={}), BASE)


def test_an_unreadable_session_is_treated_as_stale():
    """Rebuilding costs one tailor run; trusting it costs a wrong application."""
    class Broken:
        @property
        def state(self):
            raise RuntimeError("corrupt")
    assert _stale_seed(Broken(), BASE)


def test_an_empty_current_base_never_forces_a_rebuild():
    """If base.tex cannot be read, that is a separate failure. Wiping every
    session over it would re-tailor the whole backlog against nothing."""
    assert not _stale_seed(SimpleNamespace(state={"base_latex": OLD}), "")
