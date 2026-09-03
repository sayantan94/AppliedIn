"""A rebuilt résumé must arrive at a URL that has changed.

The artifact key is stable — ``resumes/<pk>.pdf`` — and re-tailoring rewrites it
in place. The server does send ``no-cache, must-revalidate`` with a real ETag,
and that layer works: with the file changed on disk from 38,708 to 38,722 bytes,
a fetch of the same URL in the browser returned the new bytes and a new ETag.

Chrome's PDF viewer is not that layer. Inside an iframe a PDF is a plugin
document, and pointing a new iframe at a URL it has already rendered lets it
reuse what it has. The owner re-tailored a résumé, opened the viewer, and read
the previous one.

So the URL carries the artifact's own mtime. A rebuilt résumé is then a
different document by its address, and nothing depends on a plugin choosing to
revalidate. Taken from the FILE rather than from a tracking field because
`tailored_at` is written once and never updated, `updated_at` is None on every
row, and `retailored_at` only marks the ones re-tailored by hand — an automatic
rebuild would have moved none of them.
"""

from __future__ import annotations

import time

from core.storage.local import FileArtifactStore


def test_the_version_changes_when_the_bytes_do(tmp_path):
    store = FileArtifactStore(str(tmp_path))
    key = store.put("resumes", "acme#1.pdf", b"first", "application/pdf")

    before = store.version(key)
    time.sleep(1.05)
    store.put("resumes", "acme#1.pdf", b"second and longer", "application/pdf")

    assert before, "an existing artifact must have a version"
    assert store.version(key) != before


def test_a_missing_artifact_has_no_version(tmp_path):
    store = FileArtifactStore(str(tmp_path))

    assert store.version("resumes/never-written.pdf") == ""


def test_a_backend_that_cannot_answer_says_so():
    """The default must be empty, not a guess: a wrong version is a URL that
    never changes again, which is the bug it exists to prevent."""
    from core.storage.base import AbstractArtifactStore

    assert AbstractArtifactStore.version(object(), "resumes/x.pdf") == ""


def test_the_link_carries_the_version(tmp_path):
    from server import _to_ui

    store = FileArtifactStore(str(tmp_path))
    key = store.put("resumes", "acme#1.pdf", b"pdf", "application/pdf")
    row = {"pk": "acme#1", "company": "Acme", "resume_s3_key": key}

    url = _to_ui(row, store)["resume_url"]

    assert url.startswith("/artifact/resumes/acme%231.pdf?v=")
    assert url.split("?v=")[1] == store.version(key)


def test_a_link_to_nothing_stays_none(tmp_path):
    from server import _to_ui

    store = FileArtifactStore(str(tmp_path))
    row = {"pk": "acme#1", "company": "Acme", "resume_s3_key": "resumes/gone.pdf"}

    assert _to_ui(row, store)["resume_url"] is None
