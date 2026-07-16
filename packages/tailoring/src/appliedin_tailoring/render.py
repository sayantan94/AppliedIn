"""Tailored resume dict -> Typst source -> PDF bytes.

``to_typst`` is pure string building (unit-testable without the binary);
``render_pdf`` shells out to the ``typst`` CLI, which the tailoring Lambda
container image bundles. A missing binary raises a clear RuntimeError instead
of a cryptic subprocess failure.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

# Characters with markup meaning in Typst; each is escaped with a backslash.
_SPECIAL = set('\\#$%&_*@~[]<>/`"')


def _esc(value: object) -> str:
    return "".join(f"\\{ch}" if ch in _SPECIAL else ch for ch in str(value))


def to_typst(tailored: dict) -> str:
    """Build a complete Typst document for the tailored resume dict."""
    lines: list[str] = [
        "#set page(margin: 1.6cm)",
        "#set text(size: 10pt)",
        "#set par(justify: true)",
        "",
    ]

    if name := tailored.get("name"):
        lines += [f"= {_esc(name)}", ""]

    contact = [tailored.get(k) for k in ("email", "phone", "location", "links")]
    contact_parts: list[str] = []
    for part in contact:
        if isinstance(part, list):
            contact_parts += [_esc(p) for p in part]
        elif part:
            contact_parts.append(_esc(part))
    if contact_parts:
        lines += [" | ".join(contact_parts), ""]

    if summary := tailored.get("summary"):
        lines += ["== Summary", _esc(summary), ""]

    if skills := tailored.get("skills"):
        rendered = ", ".join(_esc(s) for s in skills) if isinstance(skills, list) else _esc(skills)
        lines += ["== Skills", rendered, ""]

    if experience := tailored.get("experience"):
        lines.append("== Experience")
        for exp in experience:
            heading = " — ".join(
                _esc(v) for v in (exp.get("title"), exp.get("employer")) if v
            )
            dates = " – ".join(_esc(v) for v in (exp.get("start"), exp.get("end")) if v)
            lines.append(f"=== {heading}" + (f" ({dates})" if dates else ""))
            lines += [f"- {_esc(b)}" for b in exp.get("bullets") or []]
            lines.append("")

    if education := tailored.get("education"):
        lines.append("== Education")
        for edu in education:
            entry = ", ".join(
                _esc(v) for v in (edu.get("degree"), edu.get("institution")) if v
            )
            lines.append(f"- {entry}")
        lines.append("")

    if certifications := tailored.get("certifications"):
        lines.append("== Certifications")
        lines += [f"- {_esc(c)}" for c in certifications]
        lines.append("")

    return "\n".join(lines)


def render_pdf(tailored: dict) -> bytes:
    """Compile the tailored resume to PDF bytes via the ``typst`` CLI."""
    binary = shutil.which("typst")
    if binary is None:
        raise RuntimeError(
            "typst binary not found on PATH — install Typst "
            "(it is bundled into the tailoring Lambda container image)"
        )
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "resume.typ"
        out = Path(tmp) / "resume.pdf"
        src.write_text(to_typst(tailored), encoding="utf-8")
        proc = subprocess.run(
            [binary, "compile", str(src), str(out)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"typst compile failed: {proc.stderr.strip()}")
        return out.read_bytes()
