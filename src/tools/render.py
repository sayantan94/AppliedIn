"""Compile a LaTeX résumé to PDF bytes via Tectonic.

The résumé IS LaTeX (source of truth). The tailor edits the LaTeX emphasis-only;
this compiles the tailored `.tex` to a PDF with Tectonic — a single
self-contained LaTeX engine bundled into the container / installed by start.sh.
A missing binary raises a clear RuntimeError.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


def _sanitize(src: str) -> str:
    """Drop pdfTeX-only primitives so résumés written for pdflatex still compile
    with Tectonic's XeTeX engine. ``\\pdfinfo{...}`` is just PDF metadata — strip
    it (balanced braces) rather than making the user edit their base.tex."""
    out = src
    while (i := out.find(r"\pdfinfo")) != -1:
        j = out.find("{", i)
        if j == -1:
            break
        depth, k = 1, j + 1
        while k < len(out) and depth:
            depth += (out[k] == "{") - (out[k] == "}")
            k += 1
        out = out[:i] + out[k:]
    return out


def render_pdf(latex_source: str) -> bytes:
    """Compile LaTeX source to PDF bytes."""
    binary = shutil.which("tectonic")
    if binary is None:
        raise RuntimeError(
            "tectonic binary not found on PATH — install Tectonic "
            "(start.sh installs it; or `brew install tectonic`)"
        )
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "resume.tex"
        src.write_text(_sanitize(latex_source), encoding="utf-8")
        proc = subprocess.run(
            [binary, "--outdir", tmp, "--chatter", "minimal", str(src)],
            capture_output=True, text=True, check=False,
        )
        out = Path(tmp) / "resume.pdf"
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(f"tectonic compile failed: {proc.stderr.strip()[:500]}")
        return out.read_bytes()
