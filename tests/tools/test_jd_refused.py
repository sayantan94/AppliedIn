"""A posting whose page refuses a plain request is read in the browser, not closed.

The sitemap can list a job whose page sits behind the same bot wall that hid the
listing. Giving up there closed the row as unreadable; a person would open it."""

from __future__ import annotations

from tools import jd


def test_a_refused_page_is_read_in_the_browser(monkeypatch):
    monkeypatch.setattr(jd, "_from_ats", lambda url: None)
    monkeypatch.setattr(jd, "_get", lambda url, headers: None)
    asked = []
    monkeypatch.setattr(jd, "_from_chrome", lambda url, kind="jd": asked.append(kind) or
                        {"title": "Senior Engineer", "text": "Build things. " * 60})
    got = jd.fetch_jd_meta("https://jobs.acme.com/en/jobs/1/senior-engineer/", "jd_sweep")
    assert got["title"] == "Senior Engineer"
    assert asked == ["jd_sweep"]


def test_refused_companies_join_the_browser_prefetch(monkeypatch):
    from agent import run
    from core import flags

    monkeypatch.setattr(run, "_watchlist_browser_companies", lambda: {"meta"})
    monkeypatch.setattr(flags, "fetch_refused", lambda: {"fidelity"})
    assert run._browser_companies() == {"meta", "fidelity"}
