"""A bare `&` in the Skills block must be escaped, not read as a column break.

This cost four résumés and, downstream, a browser session per job. The tailor
wrote "Applied AI & Agents" with the ampersand unescaped. The repair pass exists
for exactly that, but it decided which `&` were alignment tabs by asking whether
the line ended in `\\\\` — and in this résumé the Skills block ends every line
that way as an ordinary break. So the repair ran, reported that it had changed
the document, and the compile failed on the identical error.

What follows from a failed compile is the part that hurt: only the `.tex` is
stored, `_resume_pdf_path` requires a `.pdf`, and the applier reaches the form
with nothing to upload and gates with "no tailored résumé exists for this job".
"""

from __future__ import annotations

from tools.render import sanitize_latex

DOC = r"""\documentclass{article}
\begin{document}
\begin{tabular*}{0.97\textwidth}{l@{\extracolsep{\fill}}r}
\textbf{\Large Ada Lovelace} & ada@example.com \\
\href{https://x}{x} & \href{https://y}{y} \\
\end{tabular*}
\section{Skills}
\small{
\textbf{Applied AI & Agents:} orchestration, evaluation \& guardrails.\\
\textbf{Cloud \& Infra:} AWS, Docker.\\
}
\end{document}
"""


def test_a_bare_ampersand_in_a_line_ending_in_a_break_is_escaped():
    """The Skills block ends lines with \\\\ and is not a tabular."""
    out = sanitize_latex(DOC)
    skills = next(l for l in out.splitlines() if "Applied AI" in l)
    assert r"Applied AI \& Agents" in skills, "the bare & was left as an alignment tab"


def test_separators_inside_a_real_tabular_are_left_alone():
    """Escaping these would collapse the contact header into one column."""
    out = sanitize_latex(DOC)
    header = next(l for l in out.splitlines() if "Ada Lovelace" in l)
    assert "} & ada@example.com" in header, "a real column separator was escaped"
    links = next(l for l in out.splitlines() if "href" in l and "x}" in l)
    assert "{x} & " in links


def test_an_already_escaped_ampersand_is_not_doubled():
    out = sanitize_latex(DOC)
    assert r"\\&" not in out.replace(r"\\\\", "")
    assert r"evaluation \& guardrails" in out


def test_the_repaired_document_still_has_its_structure():
    out = sanitize_latex(DOC)
    for marker in (r"\begin{tabular*}", r"\end{tabular*}", r"\section{Skills}"):
        assert marker in out
