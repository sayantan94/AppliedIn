r"""A "View résumé" button must not point at a file that isn't there.

The tracking row keeps `resume_s3_key` forever, but the bytes behind it are not
guaranteed to survive: `.local/artifacts/` was recreated on 2026-08-15 while the
rows lived on in Redis, and 127 of the 150 rows claiming a PDF lost their file.
`link()` built `/artifact/<key>` from the key alone, so the dashboard rendered
the button anyway, `openResume` took its truthy `resume_url` branch, and the
iframe loaded a 404 — a blank white panel with no error. The empty state that
says so already existed and was simply unreachable.

The applier never had this bug: `_resume_pdf_path` stats the file and returns ""
when it is gone. This makes the dashboard tell the same truth.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.storage.local import FileArtifactStore
from server import _to_ui


def _store(tmp_path: Path) -> FileArtifactStore:
    return FileArtifactStore(tmp_path)


def test_a_key_whose_file_is_gone_yields_no_url(tmp_path):
    row = {"pk": "robinhood#8088444", "resume_s3_key": "resumes/robinhood#8088444.pdf"}
    assert _to_ui(row, _store(tmp_path))["resume_url"] is None


def test_a_key_whose_file_exists_yields_a_url(tmp_path):
    store = _store(tmp_path)
    store.put("resumes", "robinhood#8120101.pdf", b"%PDF-1.4\n", "application/pdf")
    row = {"pk": "robinhood#8120101", "resume_s3_key": "resumes/robinhood#8120101.pdf"}
    # '#' must stay percent-encoded — it starts a fragment otherwise. The URL also
    # carries the artifact's version, so a re-tailored résumé is a new address and
    # Chrome's PDF viewer cannot serve the one it rendered last time.
    url = _to_ui(row, store)["resume_url"]

    assert url.startswith("/artifact/resumes/robinhood%238120101.pdf?v=")
    assert "#" not in url


def test_a_missing_screenshot_is_dropped_the_same_way(tmp_path):
    row = {"pk": "x#1", "screenshot_s3_key": "screenshots/x#1.png"}
    assert _to_ui(row, _store(tmp_path))["screenshot_url"] is None


def test_no_key_at_all_is_still_none(tmp_path):
    assert _to_ui({"pk": "x#1"}, _store(tmp_path))["resume_url"] is None
