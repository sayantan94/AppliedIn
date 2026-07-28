"""What the apply session is told about the owner's history.

Some forms make the candidate RETYPE their employment, education and skills into
structured fields rather than accepting the résumé file. The session cannot read
files, so if these extractors return nothing it reaches that page with nothing to
type, and an agent with a required field and no data is an agent about to guess.
"""

from tools.claude_chrome import _resume_history, _resume_skills

RESUME = r"""
\section{Skills}
\small{
\textbf{Applied AI \& Agents:} Agent orchestration, MCP, RAG.\\
\textbf{Languages:} Python, Go.\\
}
\section{Experience}
\resumeSubHeadingListStart
\resumeSubheading{Acme, Widgets --- Core}{}{Staff Engineer}{\textbf{Jan 2025 -- Present}}
\resumeItemListStart
\resumeItem{\textbf{Led the widget platform}, serving 500+ teams.\small{(\textit{Python, AWS})}}
\resumeItem{Cut build times by half.}
\resumeItemListEnd
\resumeSubheading{Globex}{}{Senior Engineer}{\textbf{2019 -- 2024}}
\resumeItemListStart
\resumeItem{Ran the billing pipeline.}
\resumeItemListEnd
\resumeSubHeadingListEnd
\section{Education}
\resumeSubHeadingListStart
\resumeSubheading{State University}{Springfield}{Master of Science in Computer Science}{2017 -- 2019}
\resumeSubHeadingListEnd
\section{Patents}
"""


def test_every_employer_and_the_degree_come_through():
    h = _resume_history(RESUME)
    assert "Acme" in h and "Globex" in h, "an employer left out is a gap in the form"
    assert "Staff Engineer" in h and "Senior Engineer" in h
    assert "Jan 2025 – Present" in h and "2019 – 2024" in h, "dates are separate fields"
    assert "Master of Science in Computer Science" in h
    assert "State University" in h and "2017 – 2019" in h


def test_nested_bullets_survive():
    """The richest bullet is the one nested three deep, and a regex that allows
    one level of braces drops exactly that one while looking like it worked."""
    h = _resume_history(RESUME)
    assert "Led the widget platform" in h
    assert "Cut build times by half." in h
    assert "Ran the billing pipeline." in h


def test_no_latex_leaks_into_what_gets_typed():
    """Whatever this returns may be typed into an employer's form verbatim."""
    h = _resume_history(RESUME)
    for junk in ("\\textbf", "\\resumeItem", "\\section", "{", "}"):
        assert junk not in h, f"{junk!r} would be typed into a form"


def test_sections_are_labelled_so_the_two_are_not_confused():
    h = _resume_history(RESUME)
    assert h.index("EMPLOYMENT") < h.index("Acme") < h.index("EDUCATION")


def test_skills_come_through_with_their_categories():
    s = _resume_skills(RESUME)
    assert "Agent orchestration" in s and "Python" in s
    assert "Applied AI & Agents" in s, "the category label helps pick the right chip"
    for junk in ("\\textbf", "{", "}"):
        assert junk not in s


def test_no_resume_means_no_block_rather_than_a_broken_one():
    assert _resume_history("") == ""
    assert _resume_skills("") == ""
    assert _resume_history("\\section{Experience}\n") == ""
