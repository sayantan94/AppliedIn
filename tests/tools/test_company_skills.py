"""Per-site custom instructions: matching, merging, and failing safe.

The point of the folder is that adding knowledge about a hard site is a one-file
change with no code. These tests pin the two things that makes true: a file is
found by host OR company, and a broken file never breaks an application.
"""

from __future__ import annotations

import pytest
from tools import company_skills


@pytest.fixture
def skills(tmp_path, monkeypatch):
    """A throwaway skills folder, so tests never depend on the shipped notes."""
    root = tmp_path / "company-skills"
    (root / "companies").mkdir(parents=True)
    monkeypatch.setattr(company_skills, "_skills_dir", lambda: root)
    return root


def test_no_folder_means_no_instructions(tmp_path, monkeypatch):
    monkeypatch.setattr(company_skills, "_skills_dir", lambda: tmp_path / "nope")
    assert company_skills.instructions_for("https://x.com/job", "X") == ""


def test_matches_by_host(skills):
    (skills / "ashby.md").write_text(
        "---\nname: Ashby\nmatch_hosts: [ashbyhq.com]\n---\nLocation is a combobox.")
    out = company_skills.instructions_for("https://jobs.ashbyhq.com/acme/123", "Acme")
    assert "Location is a combobox." in out
    assert "SITE-SPECIFIC RULES" in out


def test_does_not_match_an_unrelated_host(skills):
    (skills / "ashby.md").write_text(
        "---\nname: Ashby\nmatch_hosts: [ashbyhq.com]\n---\nAshby only.")
    assert company_skills.instructions_for("https://boards.greenhouse.io/x/1", "X") == ""


def test_matches_by_company_name(skills):
    (skills / "companies" / "uber.md").write_text(
        "---\nname: Uber\nmatch_companies: [uber]\n---\nFollow the Oracle redirect.")
    assert "Oracle redirect" in company_skills.instructions_for("https://any.site/job", "Uber")


def test_company_file_matches_on_filename_without_frontmatter(skills):
    """The lowest-friction way to record a lesson: a file with just prose."""
    (skills / "companies" / "acme.md").write_text("Their submit button says 'Send it'.")
    assert "Send it" in company_skills.instructions_for("https://acme.com/job", "Acme")


def test_ats_and_company_notes_are_merged_company_last(skills):
    (skills / "ashby.md").write_text(
        "---\nname: Ashby\nmatch_hosts: [ashbyhq.com]\n---\nGeneric ashby rule.")
    (skills / "companies" / "acme.md").write_text(
        "---\nname: Acme\nmatch_companies: [acme]\n---\nAcme exception.")
    out = company_skills.instructions_for("https://jobs.ashbyhq.com/acme/1", "Acme")
    assert "Generic ashby rule." in out and "Acme exception." in out
    # The company's word is the last thing the agent reads.
    assert out.index("Generic ashby rule.") < out.index("Acme exception.")


def test_allow_domains_and_success_phrases_are_collected(skills):
    (skills / "companies" / "uber.md").write_text(
        "---\nname: Uber\nmatch_companies: [uber]\n"
        "allow_domains: [oraclecloud.com]\nsuccess_phrases: ['thanks for applying']\n---\nx")
    skill = company_skills.load_skill("https://uber.com/job", "Uber")
    assert skill.allow_domains == ["oraclecloud.com"]
    assert skill.success_phrases == ["thanks for applying"]


def test_a_broken_skill_file_never_breaks_an_apply(skills):
    (skills / "broken.md").write_text("---\nmatch_hosts: [oops\n  bad: [yaml\n---\nnope")
    (skills / "good.md").write_text(
        "---\nname: Good\nmatch_hosts: [acme.com]\n---\nStill works.")
    assert "Still works." in company_skills.instructions_for("https://acme.com/j", "Acme")


def test_readme_is_not_treated_as_a_skill(skills):
    (skills / "README.md").write_text("How to add a skill: match_hosts: [acme.com]")
    assert company_skills.instructions_for("https://acme.com/job", "Acme") == ""


# --- applied-signal safety --------------------------------------------------

def test_browser_error_pages_are_never_read_as_a_confirmation():
    """The structural 'the form is gone' test cannot tell a confirmation screen
    from a page that never loaded — an error page also has no submit control, no
    inputs and a short body. A DNS failure was recorded as a submitted
    application, so positive proof of failure must veto that inference.
    """
    from tools.browser_apply import _is_error_page

    assert _is_error_page("chrome-error://chromewebdata/", "DNS_PROBE_FINISHED_NXDOMAIN")
    assert _is_error_page("https://x.example/j", "This site can't be reached")
    assert _is_error_page("https://x.example/j", "ERR_CONNECTION_REFUSED")
    assert _is_error_page("about:blank", "")
    # A real confirmation must still pass through.
    assert not _is_error_page("https://x.example/j",
                              "Thank you for applying! We have received your application.")
    assert not _is_error_page("https://x.example/j",
                              "Application Success\nYour application has been submitted")


async def test_success_wording_is_vetoed_while_the_form_still_needs_input():
    """A submitted application does not leave you looking at an empty required
    field. When both appear true, the wording came from somewhere other than a
    confirmation — a footer, a sibling posting, a pre-rendered panel — and must
    not be recorded. An uncertain apply is reviewable; a false 'applied' is never
    revisited.
    """
    from playwright.async_api import async_playwright
    from tools.browser_apply import _applied_signal, _form_still_awaiting_input

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.set_content(
                "<body><p>Thank you for applying — we review every application.</p>"
                "<form><input type='text' required>"
                "<select required><option value=''></option></select>"
                "<button>Submit application</button></form></body>")
            assert await _form_still_awaiting_input(page) is True
            assert await _applied_signal(page, "http://x/", allow_vision=False) is None

            # A real confirmation still reads as applied.
            await page.set_content(
                "<body><h1>Thank you for applying</h1><p>We received it.</p></body>")
            assert await _form_still_awaiting_input(page) is False
            assert await _applied_signal(page, "http://x/", allow_vision=False)
        finally:
            await browser.close()
