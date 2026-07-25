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


# --- choice resolution ------------------------------------------------------

def test_an_option_is_matched_by_meaning_not_substring():
    """"No" is a substring of "North Korea".

    A bare substring match once resolved a sanctions question's "No" onto the
    sanctioned-country option — the single worst wrong answer on a form.
    """
    from server import _pick_option

    sanctions = ["Citizen or permanent resident of Cuba, Iran, North Korea, or Syria",
                 "Ordinarily a resident of Russia or Belarus", "None of the above"]
    assert _pick_option("No", sanctions) == ""            # nothing means plain "No"
    assert _pick_option("None of the above", sanctions) == "None of the above"
    assert _pick_option("Yes", ["Yes", "No"]) == "Yes"
    assert _pick_option("No", ["Yes", "No"]) == "No"
    # The longer, correct option must win over a shorter accidental hit.
    assert _pick_option("none of the above", sanctions) == "None of the above"


def test_first_and_last_name_come_out_of_one_full_name():
    from server import _split_name

    assert _split_name("First Name*", "Alex Rivera") == "Alex"
    assert _split_name("Last Name*", "Alex Rivera") == "Rivera"
    assert _split_name("Preferred First Name*", "Alex Rivera") == "Alex"
    assert _split_name("Full name", "Alex Rivera") == "Alex Rivera"


# --- self-identification ----------------------------------------------------

def test_never_declares_a_protected_characteristic():
    """"Yes, I have a disability" was being ticked on the owner's behalf.

    These questions are voluntary and only the owner can answer them, so an
    affirmative self-identification is refused. The NEGATIVE answers to the same
    questions must still go through, or a required EEO field is left blank
    instead of correctly declined.
    """
    from tools.claude_chrome import _is_self_id_affirmation as declares

    assert declares("Yes, I have a disability, or have had one in the past")
    assert declares("I identify as one or more of the classifications of protected veteran")

    assert not declares("No, I do not have a disability")
    assert not declares("I am not a protected veteran")
    assert not declares("I do not wish to answer")
    # ordinary questions are untouched
    assert not declares("Are you authorized to work in the country?", "Yes")
    assert not declares("Are you able to work from our US office three days per week?", "Yes")


# --- loop engine tool guards -------------------------------------------------

def test_guards_refuse_what_a_prompt_can_only_ask_for():
    """The guarantees live in code, so a model that tries anyway is refused.

    A rule that is only a sentence in a prompt is a rule nobody enforces. These
    run on every write, whichever engine proposed it.
    """
    from tools.claude_chrome import guard_value

    assert guard_value("Legal Name", "Alex Kim") == ("Alex Kim", None)

    val, why = guard_value("Cover letter", "{{DRAFT_ESSAY_ANSWER}}")
    assert val is None and "placeholder" in why

    val, why = guard_value("Yes, I have a disability, or have had one in the past", "on")
    assert val is None and "self-identification" in why

    # The NEGATIVE answers must still go through: refusing those leaves a required
    # EEO field blank rather than correctly declined, which is a different wrong.
    assert guard_value("No, I do not have a disability", "on")[0] == "on"
    assert guard_value("I am not a protected veteran", "on")[0] == "on"


def test_a_sanctions_answer_is_corrected_not_dropped():
    """Dropping it leaves a required question blank, which fails the submit
    instead of answering it."""
    from tools.claude_chrome import guard_value

    q = "Are you a citizen of, or located in, Cuba, Iran, North Korea, or Syria?"
    val, why = guard_value(q, "Yes")
    assert val and val.lower() != "yes"
    assert why and "safe option" in why


# --- the chrome engine -------------------------------------------------------

def test_chrome_report_survives_a_nested_filled_object():
    """The report embeds a "filled" object, so brace matching has to be real.

    A regex that stops at the first closing brace truncates the report into
    nonsense and the engine reports "ended without saying what happened" for a
    run that actually succeeded.
    """
    import json as _json

    from tools.claude_chrome import _report

    env = _json.dumps({"result": 'Filled it in. {"outcome": "applied", '
                                 '"confirmation": "Thanks for applying!", '
                                 '"filled": {"Email": "a@b.c", "Phone": "555"}}'})
    report = _report(env, "outcome")
    assert report["outcome"] == "applied"
    assert report["confirmation"] == "Thanks for applying!"
    assert report["filled"]["Phone"] == "555"


def test_chrome_result_is_checked_not_trusted():
    """Acting happens in another process, so the owner's rules are re-checked.

    A rule that is only ever a sentence in a prompt is a rule nobody enforces.
    """
    from tools.claude_chrome import _verify

    ok, _ = _verify({"filled": {"Email": "a@b.c"}})
    assert ok

    ok, why = _verify({"filled": {
        "Yes, I have a disability, or have had one in the past": "on"}})
    assert not ok and "protected characteristic" in why

    ok, why = _verify({"filled": {"Cover letter": "{{DRAFT_ESSAY_ANSWER}}"}})
    assert not ok and "placeholder" in why


def test_chrome_engine_scopes_itself_to_browser_tools():
    """Filling a form must not come with bash, Read, or Edit.

    Write is granted only so the task can hand back a structured report, and
    --add-dir confines it to a scratch directory, so no repo file is reachable.
    """
    from tools.claude_chrome import ALLOWED_TOOLS

    assert "mcp__claude-in-chrome" in ALLOWED_TOOLS
    assert set(ALLOWED_TOOLS) <= {"mcp__claude-in-chrome", "Write"}
    for forbidden in ("Bash", "Read", "Edit", "WebFetch"):
        assert forbidden not in ALLOWED_TOOLS


def test_applied_is_never_recorded_without_the_page_saying_so():
    """Two engines have reported a submission that never happened.

    One read a DNS error page as a confirmation; another read "Thank you for
    applying" off a page whose form was still empty. So claiming success is not
    enough — the page has to have said something.
    """
    from tools.claude_chrome import classify

    assert classify({"outcome": "applied"})["status"] == "unknown"
    assert classify({"outcome": "applied", "confirmation": "   "})["status"] == "unknown"

    ok = classify({"outcome": "applied", "confirmation": "Application Success"})
    assert ok["status"] == "applied" and ok["confirmation"] == "Application Success"


def test_a_block_is_named_so_the_owner_knows_what_to_do():
    """"Already applied" and "you have used your 5 applications" need different
    responses, so reporting both as a generic failure is not good enough."""
    from tools.claude_chrome import classify

    assert classify({"outcome": "blocked",
                     "blocked_by": "application_limit"})["reason"] == "application_limit"
    assert classify({"outcome": "blocked",
                     "blocked_by": "already_applied"})["reason"] == "already_applied"
    # An unrecognised reason is reported plainly, never reinterpreted.
    assert classify({"outcome": "blocked", "blocked_by": "vibes"})["reason"] == "blocked"
    assert classify({"outcome": "blocked", "detail": "something odd"})["reason"] == "blocked"


def test_a_guardrail_breach_is_never_recorded_as_applied():
    """Even with a confirmation, an application that declared a protected
    characteristic is refused rather than filed."""
    from tools.claude_chrome import classify

    res = classify({"outcome": "applied", "confirmation": "Thanks!",
                    "filled": {"Yes, I have a disability, or have had one in the past": "on"}})
    assert res["status"] == "failed" and res["reason"] == "guardrail"
