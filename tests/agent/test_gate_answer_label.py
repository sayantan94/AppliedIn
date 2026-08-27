"""A gate answer must be filed under the FIELD, not under the agent's paragraph.

The applier does not ask "what is your desired salary range?". It asks:

    Replit's application for Forward Deployed Engineer (Foster City, CA / NYC)
    has a required field: 'What is your desired salary range?' The posted range
    for the role is $180K-$300K plus equity. I have no approved desired-salary
    figure to enter, so I filled in everything else and left the form ready to
    submit as soon as you tell me what number/range to use.

That whole paragraph was the answer-bank key. The bank is keyed on the
normalized form label, so nothing ever looked it up again — the next session
read a form field called "What is your desired salary range?", found no key
like it, and gated a second time with the same question in different words.

The paragraph is also worth 300 characters of the prompt on every later
application, for a key nothing queries. The field label is the reusable part, so
the label is what gets stored.
"""

from __future__ import annotations

import pytest

from agent.run import _gate_label

REPLIT = ("Replit's application for Forward Deployed Engineer (Foster City, CA / "
          "NYC) has a required field: 'What is your desired salary range?' The "
          "posted range for the role is $180K-$300K plus equity. I have no "
          "approved desired-salary figure to enter, so I filled in everything "
          "else and left the form ready to submit as soon as you tell me what "
          "number/range to use.")


def test_the_quoted_field_label_is_what_gets_stored():
    assert _gate_label(REPLIT) == "What is your desired salary range?"


def test_double_quotes_work_too():
    q = 'The form has a required field: "What is your notice period?" — no answer.'

    assert _gate_label(q) == "What is your notice period?"


def test_the_longest_quoted_label_wins():
    """Agents quote the role and the field in the same breath. The field is the
    specific one, and the specific one is the answer's real key."""
    q = ("For 'Staff Engineer' the form asks 'How many years of professional "
         "experience do you have with distributed systems?' and I have no answer.")

    assert _gate_label(q) == ("How many years of professional experience do you "
                             "have with distributed systems?")


@pytest.mark.parametrize("q", [
    "Ready to apply to Replit? The tailored résumé is saved — approve to submit.",
    "The portal wants a security code sent to your phone. What is it?",
    "I clicked 'Submit' but the page did not change.",
    "",
])
def test_no_label_rather_than_a_wrong_one(q):
    """A bare button name is not a question the owner answered. When nothing in
    the gate looks like a form field, the caller keeps today's behaviour and
    banks the whole question — a useless key is survivable, a wrong one is not."""
    assert _gate_label(q) is None
