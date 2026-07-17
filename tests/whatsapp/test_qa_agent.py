"""Q&A agent: strictly read-only toolset; strands never imported in tests."""

from __future__ import annotations

import sys

from core.models import JobRecord, Status
from core.storage.artifacts import ArtifactStore
from core.storage.tracking import TrackingStore
from whatsapp.qa_agent import answer, build_tools

# Any of these in a tool name would mean the agent can change state.
MUTATION_TOKENS = (
    "put", "set", "write", "delete", "update", "create", "enqueue",
    "seed", "increment", "post", "send", "remove", "insert",
)


def test_toolset_contains_no_writer(applications_table, artifacts_bucket):
    tools = build_tools(TrackingStore(applications_table), ArtifactStore(artifacts_bucket))
    names = [t.__name__ for t in tools]

    assert names == ["get_application", "list_applications_by_status", "presign_artifact"]
    for name in names:
        for token in MUTATION_TOKENS:
            assert token not in name.lower(), f"mutation-looking tool {name!r}"


def test_tools_read_real_state(applications_table, artifacts_bucket):
    tracking = TrackingStore(applications_table)
    artifacts = ArtifactStore(artifacts_bucket)
    job = JobRecord(company="Acme", job_id="1", title="SWE", jd_url="u",
                    jd_text="x", location="R", ats="greenhouse")
    tracking.put_new(job)
    tracking.set_status(job.pk, Status.SKIPPED, skip_reason="low score")
    key = artifacts.put("resumes", "acme#1.pdf", b"%PDF-fake", "application/pdf")

    get_application, list_by_status, presign = build_tools(tracking, artifacts)

    assert get_application(job.pk)["skip_reason"] == "low score"
    assert [r["pk"] for r in list_by_status("skipped")] == [job.pk]
    assert "acme%231.pdf" in presign(key) or "acme#1.pdf" in presign(key)


def test_answer_uses_injected_agent_and_only_read_tools(applications_table, artifacts_bucket):
    tracking = TrackingStore(applications_table)
    artifacts = ArtifactStore(artifacts_bucket)
    job = JobRecord(company="Acme", job_id="1", title="SWE", jd_url="u",
                    jd_text="x", location="R", ats="greenhouse")
    tracking.put_new(job)
    tracking.set_status(job.pk, Status.SKIPPED, skip_reason="below threshold")

    captured = {}

    class FakeAgent:
        def __init__(self, *, model, tools, system_prompt):
            captured["model"] = model
            captured["tools"] = tools
            captured["system_prompt"] = system_prompt

        def __call__(self, question):
            # Behave like the real agent would: answer from the read tool.
            get_application = captured["tools"][0]
            row = get_application("acme#1")
            return f"acme#1 was skipped: {row['skip_reason']}"

    sentinel_model = object()
    reply = answer(
        "why was the Acme one skipped?",
        tracking=tracking,
        artifacts=artifacts,
        model=sentinel_model,
        agent_factory=FakeAgent,
    )

    assert reply == "acme#1 was skipped: below threshold"
    assert captured["model"] is sentinel_model  # injected, never Bedrock
    assert [t.__name__ for t in captured["tools"]] == [
        "get_application", "list_applications_by_status", "presign_artifact",
    ]
    assert "read-only" in captured["system_prompt"]
    assert "strands" not in sys.modules  # SDK stays out of the test process
