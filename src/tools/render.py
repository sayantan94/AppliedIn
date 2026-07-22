"""Compile a LaTeX résumé to PDF bytes.

The résumé IS LaTeX (source of truth). Prefer **pdflatex** when it's installed —
résumés are written for it, so `times`, `\\pdfinfo`, and `\\textbf` all render
exactly as the original. Fall back to **Tectonic** (self-contained XeTeX) with a
sanitize pass: XeTeX lacks `\\pdfinfo` and the pdflatex-era `times` package
doesn't render bold, so we strip the former and swap the latter for `newtxtext`
(a Times clone that keeps bold/italic).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def _strip_pdfinfo(src: str) -> str:
    """Remove ``\\pdfinfo{...}`` (a pdfTeX-only primitive XeTeX chokes on)."""
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


def _sanitize(src: str) -> str:
    """Make a pdflatex résumé compile with Tectonic's XeTeX engine AND keep bold:
    drop ``\\pdfinfo`` and swap the `times` package for `newtxtext` (Times clone
    with a real bold series — plain `times` renders \\textbf as regular in XeTeX)."""
    out = _strip_pdfinfo(src)
    out = "\n".join(
        re.sub(r"\btimes\b", "newtxtext", line) if "\\usepackage" in line else line
        for line in out.splitlines()
    )
    return out


def _run(cmd: list[str], tmp: str, name: str) -> bytes:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    out = Path(tmp) / f"{name}.pdf"
    if not out.exists():
        tail = (proc.stdout or proc.stderr).strip()[-500:]
        raise RuntimeError(f"LaTeX compile failed: {tail}")
    return out.read_bytes()


def sanitize_latex(tex: str) -> str:
    """Escape the LaTeX specials models keep writing bare in résumé BULLETS —
    `$500K`, `30% faster`, `AT&T`, `#1` — which break compilation (a naked `%`
    even silently comments out the rest of the line). Context-aware and
    body-only: the preamble's real `%` comments and tabular `&` separators are
    untouched, as is anything already escaped. Only used as a compile-failure
    retry, so a document that compiles is never rewritten."""
    import re

    marker = "\\begin{document}"
    at = tex.find(marker)
    if at == -1:
        return tex
    head, body = tex[: at + len(marker)], tex[at + len(marker):]
    body = re.sub(r"(?<!\\)\$", r"\\$", body)          # $500K → \$500K
    body = re.sub(r"(?<=\d)\s?%", r"\\%", body)        # 30% → 30\% (digit-bound only)
    body = re.sub(r"(?<=\w)&(?=\w)", r"\\&", body)     # AT&T → AT\&T (word-bound only)
    body = re.sub(r"(?<!\\)#(?=\d)", r"\\#", body)     # #1 → \#1
    return head + body


def render_pdf(latex_source: str) -> bytes:
    """Compile LaTeX source to PDF bytes (pdflatex if available, else Tectonic)."""
    if (pdflatex := shutil.which("pdflatex")) is not None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "resume.tex").write_text(latex_source, encoding="utf-8")
            return _run([pdflatex, "-interaction=nonstopmode", "-halt-on-error",
                         "-output-directory", tmp, str(Path(tmp) / "resume.tex")], tmp, "resume")
    if (tectonic := shutil.which("tectonic")) is not None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "resume.tex"
            src.write_text(_sanitize(latex_source), encoding="utf-8")
            return _run([tectonic, "--outdir", tmp, "--chatter", "minimal", str(src)],
                        tmp, "resume")
    raise RuntimeError("no LaTeX engine on PATH — install pdflatex (TeX Live) or tectonic")
