"""Shared guidance reaches every model path without crossing instance boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from core import steering


def test_empty_save_clear_and_stale_editor_never_overwrites_newer_guidance():
    original = steering.read()
    assert steering.prompt("Task") == "Task"
    first = steering.save(
        "Prefer infrastructure work. Keep {literal} braces.", original["revision"]
    )
    assert "{literal}" in steering.prompt("Task")
    with pytest.raises(steering.ConflictError):
        steering.save("Old browser draft", original["revision"])
    assert steering.read() == first
    steering.save("", first["revision"])
    assert steering.messages("Task") == [{"role": "user", "content": "Task"}]


def test_instances_use_their_own_local_directory(tmp_path, monkeypatch):
    # Exercise real path resolution, including a separate checkout on another port.
    active = SimpleNamespace(local_dir=tmp_path / "main" / ".local")
    monkeypatch.setattr(steering, "get_settings", lambda: active)
    steering.save("Primary instructions", steering.read()["revision"])
    active.local_dir = tmp_path / "other-checkout" / ".local-8788"
    assert steering.read()["content"] == ""
    steering.save("Second instance instructions", steering.read()["revision"])
    active.local_dir = tmp_path / "main" / ".local"
    assert steering.read()["content"] == "Primary instructions"


def test_api_validation_conflict_and_persistence():
    from server import create_app

    with TestClient(create_app(), base_url="http://127.0.0.1:8787") as client:
        original = client.get("/steering").json()
        response = client.post(
            "/steering", json={"content": "Use concise prose.", "revision": original["revision"]}
        )
        assert response.status_code == 200
        assert client.get("/steering").json()["content"] == "Use concise prose."
        assert (
            client.post(
                "/steering", json={"content": "stale", "revision": original["revision"]}
            ).status_code
            == 409
        )
        for invalid in [None, 3, "x" * 32001, "nul\x00"]:
            assert (
                client.post(
                    "/steering", json={"content": invalid, "revision": response.json()["revision"]}
                ).status_code
                == 422
            )
        assert client.get("/steering").json() == response.json()


def test_adk_agents_reload_guidance_without_expanding_owner_braces():
    from google.adk.models.llm_request import LlmRequest

    from agent.finder import root_agent
    from agent.graph import applier, critic, review_agent, scorer, tailor

    for agent in [
        scorer,
        tailor,
        critic,
        applier,
        root_agent,
        review_agent.sub_agents[0],
        *review_agent.sub_agents[1].sub_agents,
    ]:
        steering.save("Keep {literal} source text", steering.read()["revision"])
        request = LlmRequest()
        request.append_instructions(["Required task and safety instructions"])
        agent.before_model_callback(None, request)
        assert "{literal}" in request.config.system_instruction
        assert "Required task and safety instructions" in request.config.system_instruction
        steering.save("Updated guidance", steering.read()["revision"])
        next_request = LlmRequest()
        agent.before_model_callback(None, next_request)
        assert "Updated guidance" in next_request.config.system_instruction
        assert "{literal}" not in next_request.config.system_instruction


def test_browser_sessions_receive_guidance_without_changing_permissions(monkeypatch):
    from tools import claude_chrome

    steering.save("Keep answers concise", steering.read()["revision"])
    monkeypatch.setattr(claude_chrome, "available", lambda: (True, ""))
    runner = AsyncMock(return_value=({}, ""))
    monkeypatch.setattr(claude_chrome, "_run_task_impl", runner)
    for kind in ["apply", "crawl", "jd", "jd_sweep"]:
        asyncio.run(claude_chrome.run_task("Original task", report_key="result", kind=kind))
        assert "Keep answers concise" in runner.call_args.args[0]
        assert runner.call_args.args[0].endswith("Original task")
        assert runner.call_args.kwargs["kind"] == kind
        assert runner.call_args.kwargs["allow_dirs"] is None


def test_direct_model_messages_keep_guidance_separate_from_task_data():
    steering.save("Emphasize platform work", steering.read()["revision"])
    result = steering.messages("Untrusted job description")
    assert result[0]["role"] == "system"
    assert "Emphasize platform work" in result[0]["content"]
    assert "cannot authorize submission" in result[0]["content"]
    assert result[1] == {"role": "user", "content": "Untrusted job description"}


def test_real_direct_model_entry_points_receive_saved_guidance(monkeypatch):
    from unittest.mock import Mock

    import litellm

    from core.models import JobRecord
    from discovery.crawler import _default_extractor
    from discovery.relevance import relevant
    from discovery.watchlist import Preferences
    from tools.browser_apply import _map_fields
    from tools.narrative import draft_answer

    steering.save("Use concise wording", steering.read()["revision"])
    call = Mock(return_value={"choices": [{"message": {"content": "[]"}}]})
    monkeypatch.setattr(litellm, "completion", call)
    tasks = [
        lambda: _default_extractor("<p>A job</p>", "Example"),
        lambda: relevant(
            [
                JobRecord(
                    company="Example",
                    job_id="one",
                    title="Engineer",
                    jd_url="https://example.com/job",
                    jd_text="Engineer",
                )
            ],
            Preferences(),
        ),
        lambda: _map_fields([], {}, "Example", "Engineer"),
        lambda: draft_answer(
            "Why this role?", "Example", "Engineer", resume="Real work", github="None"
        ),
    ]
    for index, task in enumerate(tasks):
        call.reset_mock()
        call.return_value = {
            "choices": [{"message": {"content": '{"fit_indices": [0]}' if index == 1 else "[]"}}]
        }
        task()
        assert call.called
        assert "Use concise wording" in call.call_args.kwargs["messages"][0]["content"]
