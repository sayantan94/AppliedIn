"""Deterministic truthfulness validator for LaTeX résumés (HLD guardrail 4).

The tailor may reorder/reword the bullets (`\\resumeItem`) and the summary —
that's the point — but never touch the structural FACTS. Those live in the
`\\resumeSubheading{...}` / `\\resumeSubheadingSingle{...}` lines (employer,
title, dates, project). We take each such line from the base `.tex` as an
anchor and require it to survive verbatim in the tailored `.tex`. Reordering
experiences is fine (every anchor still appears); altering or dropping an
employer/title/date is not.

No separate facts file — the anchors come straight from your base.tex. If your
résumé uses different macros, add them to ``_ANCHOR_PREFIXES``.
"""

from __future__ import annotations

import re

_ANCHOR_PREFIXES = ("\\resumeSubheading", "\\resumeSubheadingSingle")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _anchors(base_latex: str) -> list[str]:
    """The structural lines whose facts must survive verbatim."""
    out = []
    for line in base_latex.splitlines():
        s = line.strip()
        if s.startswith(_ANCHOR_PREFIXES) and len(s) > len("\\resumeSubheading") + 2:
            out.append(s)
    return out


def validate(base_latex: str, tailored_latex: str) -> list[str]:
    """Return the structural facts the tailored résumé dropped/altered.

    Empty list == truthful. Whitespace-insensitive so reflowed LaTeX matches;
    order-insensitive so reordering experiences is allowed.
    """
    hay = _norm(tailored_latex)
    return [a for a in _anchors(base_latex) if _norm(a) not in hay]
