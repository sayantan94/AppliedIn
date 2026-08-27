"""Every approved answer must reach the browser, in full, especially the newest.

The block of approved answers was rendered as ``json.dumps(facts)[:9000]``. The
real block is ~47,000 characters, so 52 of the owner's 137 answers were cut off
and the browser model never saw them. Which 52 was not random: ``all_facts``
returns global scope then company scope, and a new answer is APPENDED to its
scope, so the answers cut were always the most recently added ones.

That is the exact shape of the bug the owner reported. He answered "What is your
desired salary range?" at a Replit gate, the answer was banked, the job was
re-queued, and five minutes later the same session gated again saying "I have no
approved desired-salary figure to enter". It had not ignored the answer; the
answer was at character 46,460 of a string cut at 9,000.

There is no cap now. The whole bank is roughly twelve thousand tokens against a
million-token context, and the cost of dropping a single answer is a gate the
owner already answered, asked again on every job at that company.
"""

from __future__ import annotations

from tools.claude_chrome import _answer_block

ESSAY = "x" * 1400


def _bulk(n: int) -> dict:
    return {f"Question number {i}?": f"answer {i}" for i in range(n)}


def test_a_freshly_banked_answer_survives_a_full_bank():
    """The regression: the newest answer is last, and last is what got cut."""
    facts = {**_bulk(60), **{f"Essay {i}?": ESSAY for i in range(30)},
             "What is your desired salary range?": "300K base"}

    block = _answer_block(facts)

    assert "What is your desired salary range?" in block
    assert "300K base" in block


def test_nothing_is_dropped_however_large_the_bank_gets():
    """A missing key reads to the model as "the owner never answered this", which
    is a false gate on an application he could have had."""
    facts = {**_bulk(500), **{f"Essay {i}?": ESSAY for i in range(300)}}

    block = _answer_block(facts)

    for key, value in facts.items():
        assert key in block, f"{key!r} was dropped from the block"
        assert value in block, f"the answer to {key!r} was cut"


def test_long_prose_is_carried_verbatim():
    """Stored prose is source material for free-text fields. A paragraph that
    arrives half-written is worse than one that arrives whole."""
    facts = {f"Essay {i}?": ESSAY for i in range(200)}

    block = _answer_block(facts)

    assert block.count(ESSAY) == 200


def test_keys_read_as_the_owner_wrote_them():
    """The model matches these keys against labels it reads off the page, so an
    em dash has to survive as an em dash, not as an escape sequence."""
    facts = {"Gender (EEO — optional)": "Male"}

    block = _answer_block(facts)

    assert "Gender (EEO — optional)" in block
    assert "\\u2014" not in block


def test_an_empty_answer_is_not_offered_as_one():
    """A key with a blank value is not an answer; handing it over invites the
    model to treat the field as settled and type nothing into it."""
    facts = {"Full name": "Sayantan Bhowmik", "Veteran status": "  ", "Degree": ""}

    block = _answer_block(facts)

    assert "Full name" in block
    assert "Veteran status" not in block
    assert "Degree" not in block
