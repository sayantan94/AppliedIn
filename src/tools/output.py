"""Per-job output folder — a human-browsable record of each application.

For every job the pipeline runs, we drop a timestamped folder under ``output/``
holding the fetched JD, the tailored résumé (.tex + .pdf when it rendered), and
a short job.md. Git-ignored; purely for you to inspect what the pipeline
produced. One folder per job run, named by time + company + job id.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from core.logging import get_logger

log = get_logger(__name__)

_ROOT = Path(__file__).resolve().parents[2] / "output"


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s or "").strip("-")[:48] or "job"


def write_job_output(
    pk: str,
    *,
    company: str = "",
    title: str = "",
    url: str = "",
    score: object = None,
    jd_text: str = "",
    tex: str | None = None,
    pdf: bytes | None = None,
    when: datetime | None = None,
) -> Path | None:
    """Write everything we have for one job into output/<stamp>_<company>_<id>/.
    Returns the folder path (or None if writing failed — never fatal)."""
    try:
        stamp = (when or datetime.now()).strftime("%Y-%m-%d_%H%M%S")
        folder = _ROOT / f"{stamp}_{_slug(company)}_{_slug(pk)}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "job.md").write_text(
            f"# {title or pk}\n\n"
            f"- **Company:** {company}\n- **Job id:** {pk}\n- **URL:** {url}\n"
            f"- **Match score:** {score if score is not None else '—'}\n"
            f"- **Saved:** {stamp}\n"
        )
        if jd_text:
            (folder / "jd.txt").write_text(jd_text)
        if tex:
            (folder / "resume.tex").write_text(tex)
        if pdf:
            (folder / "resume.pdf").write_bytes(pdf)
        log.info("saved job output → %s", folder)
        return folder
    except Exception as exc:  # inspection nicety, never break the run
        log.warning("could not write job output for %s: %s", pk, exc)
        return None
