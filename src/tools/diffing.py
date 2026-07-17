"""What the tailor changed — a bullet-level before/after of the résumé.

Compares the seed résumé's ``\\resumeItem`` bullets to the tailored ones and
returns the changed pairs, so the dashboard can show exactly what was reworded
(and confirm the structural facts were left alone).
"""

from __future__ import annotations

import difflib
import re

_ITEM = re.compile(r"\\resumeItem\{(.+)\}\s*$")


def _bullets(tex: str) -> list[str]:
    """The text inside each single-line ``\\resumeItem{...}``."""
    return [m.group(1).strip() for line in tex.splitlines() if (m := _ITEM.search(line.strip()))]


def _clean(s: str) -> str:
    """Strip the common LaTeX wrappers so the diff reads as plain prose."""
    s = re.sub(r"\\textbf\{(.+?)\}", r"\1", s)
    s = re.sub(r"\\textit\{(.+?)\}", r"\1", s)
    s = re.sub(r"\\href\{.+?\}\{(.+?)\}", r"\1", s)
    s = s.replace("\\&", "&").replace("\\%", "%").replace("~", " ")
    return s.strip()


def resume_diff(base_latex: str, tailored_latex: str) -> list[dict]:
    """Return the reworded bullets as ``{type, before, after}`` entries
    (type ∈ replace/insert/delete). Empty if nothing meaningful changed."""
    a, b = _bullets(base_latex), _bullets(tailored_latex)
    changes: list[dict] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        changes.append({
            "type": tag,
            "before": [_clean(x) for x in a[i1:i2]],
            "after": [_clean(x) for x in b[j1:j2]],
        })
    return changes
