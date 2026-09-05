"""A posting we cannot read must not become a job description.

metacareers.com answers 400 to any request carrying browser-like headers — the
disguise is what trips it — and 200 with a JavaScript shell to a plain one.
Either way there is no description in the response. What came back instead was
the error page:

    "Error. Sorry, something went wrong. We're working on getting this fixed..."

126 characters, and nothing rejected it. `fetch_jd` returned it, `_jd_text`
preferred it over whatever discovery had captured, and the tailor would have
written a résumé against an error page and queued it for an employer.

Three guards, in the order they fire:
  the fetcher    a non-2xx response is a refusal, not a page — return nothing
  the chooser    never replace what we have with something worse
  the pipeline   too little to tailor from is a reason to stop, not to guess
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from agent.run import _jd_text, _unreadable
from tools.jd import fetch_jd_meta

ERROR_PAGE = (
    "<html><head><title>Meta Careers</title></head><body>"
    "<p>Error</p><p>Sorry, something went wrong.</p>"
    "<p>We're working on getting this fixed as soon as we can.</p></body></html>"
)
REAL_JD = "<html><body><h1>Staff Engineer</h1><p>" + ("Build distributed systems. " * 40) + "</p></body></html>"


_RealClient = httpx.Client


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    from tools import jd as _jd
    monkeypatch.setattr(_jd, "_from_chrome", lambda url, kind="jd": None)


def _transport(handler):
    return httpx.MockTransport(handler)


def _patched(handler):
    return lambda **kw: _RealClient(transport=_transport(handler), **kw)


def test_a_refusal_is_not_a_posting(monkeypatch):
    def handler(request):
        return httpx.Response(400, text=ERROR_PAGE)

    monkeypatch.setattr(httpx, "Client", _patched(handler))

    got = fetch_jd_meta("https://www.metacareers.com/profile/job_details/1")

    assert got["text"] == ""
    assert "something went wrong" not in got["text"]


def test_the_disguise_is_dropped_when_it_is_what_got_refused(monkeypatch):
    """Measured: browser-like headers get 400, no headers get 200. So a refusal
    is retried once, bare, before giving up."""
    seen = []

    def handler(request):
        seen.append(dict(request.headers))
        if "user-agent" in {k.lower() for k in request.headers} and \
                "python" not in request.headers.get("user-agent", "").lower():
            return httpx.Response(400, text=ERROR_PAGE)
        return httpx.Response(200, text=REAL_JD)

    monkeypatch.setattr(httpx, "Client", _patched(handler))

    got = fetch_jd_meta("https://www.metacareers.com/profile/job_details/1")

    assert len(seen) == 2, "the refused request should be retried without the disguise"
    assert "distributed systems" in got["text"]


def test_a_readable_posting_is_untouched(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _patched(lambda r: httpx.Response(200, text=REAL_JD)))

    got = fetch_jd_meta("https://example.com/jobs/1")

    assert "distributed systems" in got["text"]
    assert got["title"] == "Staff Engineer"


def test_the_chooser_never_downgrades(monkeypatch):
    """Discovery's listing summary beats an error page. `_jd_text` used to take
    whatever the fetch returned, so a 126-character refusal replaced it."""
    monkeypatch.setattr("tools.jd.fetch_jd", lambda url, kind="jd": "Sorry, something went wrong.")
    row = {"jd_url": "https://x/1", "jd_text": "A" * 300}

    assert asyncio.run(_jd_text(row)) == "A" * 300


def test_the_chooser_takes_a_real_fetch(monkeypatch):
    monkeypatch.setattr("tools.jd.fetch_jd", lambda url, kind="jd": "B" * 900)
    row = {"jd_url": "https://x/1", "jd_text": "A" * 300}

    assert asyncio.run(_jd_text(row)) == "B" * 900


@pytest.mark.parametrize("text", ["", "   ", "Sorry, something went wrong.", "Production Engineering"])
def test_too_little_to_tailor_from(text):
    assert _unreadable(text) is True


@pytest.mark.parametrize("text", ["We are looking for a staff engineer. " * 10])
def test_enough_to_tailor_from(text):
    assert _unreadable(text) is False
