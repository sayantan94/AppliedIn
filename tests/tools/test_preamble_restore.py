r"""The preamble belongs to the seed, not to the tailor.

The Robinhood résumé of 2026-08-21 went out with every job title reading
"textitSenior System / Software Engineer". The tailor re-emitted the whole
document and doubled the backslashes inside the `\resumeSubheading` macro
definition — `#2 \\\textit{\small#3}` became `#2 \\\\textit{\small#3}`. That is
*valid* LaTeX: two row breaks, then the literal word "textit". So every guard
we had waved it through — `validate()` only anchors on `\resumeSubheading{...}`
call sites and never on the macro's definition, the bullet count was unchanged,
and Tectonic compiled it happily. A PDF with "textit" glued to five headings
was uploaded and queued for an employer.

The rule "leave the preamble untouched, byte-for-byte" lived only in SKILL.md.
A prompt is not a guard. The seed's preamble is authoritative by definition, so
the tool layer puts it back.
"""

from __future__ import annotations

from tools.render import restore_preamble

SEED = "\n".join([
    r"\documentclass[letterpaper,11pt]{article}",
    r"\usepackage{titlesec}",
    r"\newcommand{\resumeSubheading}[4]{\item\textbf{#1} & #2 \\\textit{\small#3} \\",
    r"}",
    r"\begin{document}",
    r"\resumeSubheading{Acme}{2020--2024}{Senior Engineer}{Seattle, WA}",
    r"\resumeItem{Built the thing.}",
    r"\end{document}",
    "",
])


def _swap_body(doc: str, old: str, new: str) -> str:
    return doc.replace(old, new)


def test_a_corrupted_macro_definition_is_replaced_by_the_seeds():
    """The exact Robinhood corruption: `\\\\` doubled inside the macro."""
    tailored = SEED.replace(r"#2 \\\textit", r"#2 \\\\textit")
    out = restore_preamble(SEED, tailored)
    assert r"#2 \\\textit{\small#3}" in out, "the doubled backslashes survived"
    assert r"\\\\textit" not in out


def test_the_tailored_body_is_kept_verbatim():
    """Restoring the preamble must not undo the tailoring itself."""
    tailored = _swap_body(SEED, r"\resumeItem{Built the thing.}",
                          r"\resumeItem{Architected the thing at scale.}")
    out = restore_preamble(SEED, tailored)
    assert r"\resumeItem{Architected the thing at scale.}" in out
    assert r"\resumeItem{Built the thing.}" not in out


def test_an_untouched_preamble_round_trips_byte_for_byte():
    assert restore_preamble(SEED, SEED) == SEED


def test_a_document_without_the_marker_is_left_alone():
    """No \\begin{document} on either side means we cannot tell head from body."""
    assert restore_preamble(SEED, "fragment") == "fragment"
    assert restore_preamble("fragment", SEED) == SEED
