"""Career-site crawler: extraction -> predicate -> dedup put_new -> enqueue."""

from __future__ import annotations

from appliedin_core.config import Settings
from appliedin_core.models import JobRecord, Status
from appliedin_core.storage.tracking import TrackingStore
from appliedin_worker.crawl_main import CrawlDeps, parse_postings, run_crawl


class FakePage:
    def __init__(self, html):
        self._html = html
        self.visited = []

    async def goto(self, url):
        self.visited.append(url)

    async def content(self):
        return self._html


class FakeQueue:
    def __init__(self):
        self.messages = []

    def enqueue(self, url, body):
        self.messages.append((url, body))
        return "mid"


def _record(job_id, title):
    return JobRecord(
        company="Acme", job_id=job_id, title=title, jd_url=f"https://acme/{job_id}",
        jd_text=f"{title} role", location="Remote", ats="custom",
    )


def _deps(tracking, queue, extractor, matches, page):
    return CrawlDeps(
        tracking=tracking,
        queue=queue,
        settings=Settings(tailor_queue_url="tailor-url"),
        extractor=extractor,
        matches=matches,
        page=page,
    )


async def test_crawl_extracts_filters_dedups_and_enqueues(applications_table):
    tracking = TrackingStore(applications_table)
    queue = FakeQueue()
    page = FakePage("<html>careers</html>")
    extracted = [
        _record("1", "Software Engineer"),
        _record("2", "Staff Engineer"),
        _record("3", "Account Executive"),  # filtered out by the predicate
    ]

    def extractor(html, cfg):
        assert html == "<html>careers</html>" and cfg["name"] == "Acme"
        return extracted

    deps = _deps(tracking, queue, extractor, lambda j: "Engineer" in j.title, page)
    enqueued = await run_crawl(
        {"name": "Acme", "careers_url": "https://acme/careers"}, deps=deps
    )

    assert page.visited == ["https://acme/careers"]
    assert enqueued == 2
    assert [b for _, b in queue.messages] == [{"pk": "acme#1"}, {"pk": "acme#2"}]
    assert all(u == "tailor-url" for u, _ in queue.messages)
    row = tracking.get("acme#1")
    assert row["status"] == Status.FOUND.value

    # Second crawl: conditional put_new blocks every duplicate -> nothing enqueued.
    enqueued_again = await run_crawl(
        {"name": "Acme", "careers_url": "https://acme/careers"}, deps=deps
    )
    assert enqueued_again == 0
    assert len(queue.messages) == 2


def test_parse_postings_is_defensive():
    reply = (
        "Here are the postings:\n"
        '[{"job_id": "7", "title": "SWE", "jd_url": "https://x/7", '
        '"location": "Remote", "jd_text": "build"},'
        ' {"title": "missing id"}, "junk"]'
    )
    records = parse_postings(reply, {"name": "Acme", "ats": "custom"})
    assert len(records) == 1
    assert records[0].pk == "acme#7"
    assert records[0].ats == "custom"

    assert parse_postings("no array", {"name": "Acme"}) == []
    assert parse_postings("[{bad json", {"name": "Acme"}) == []
