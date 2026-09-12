"""Career Ops uses subscription auth and grounds jobs only in read-only tool results."""

import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from discovery import claude_search as cs

URL = "https://jobs.ashbyhq.com/acme/real-posting"
INVENTED = "https://jobs.ashbyhq.com/acme/invented"


def events():
    return [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "private reasoning"},
                    {
                        "type": "tool_use",
                        "id": "search1",
                        "name": "WebSearch",
                        "input": {"query": "software engineer Seattle"},
                    },
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "search1",
                        "content": f'Links: [{{"title":"Engineer","url":"{URL}"}}]',
                    },
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": f"Proposed result: {INVENTED}"},
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": {"summary": "A match", "jobs": []},
        },
    ]


def test_stream_reveals_queries_before_completion_but_not_reasoning_or_invented_links():
    progress = []
    parser = cs.SearchEvents(progress.append)
    for event in events()[:-1]:
        parser.accept(event)
    assert "Searching: software engineer Seattle" in progress
    assert "private reasoning" not in " ".join(progress)
    assert parser.result is None
    parser.accept(events()[-1])
    assert parser.finish()["source_urls"] == {URL}


def test_failed_or_unrelated_tools_do_not_ground_postings():
    parser = cs.SearchEvents(lambda message: None)
    sequence = events()
    sequence[1]["message"]["content"][0]["is_error"] = True
    for event in sequence:
        parser.accept(event)
    assert not parser.sources
    with pytest.raises(ValueError, match="did not run a web search"):
        parser.finish()
    sequence[1]["message"]["content"][0].update(tool_use_id="unknown", is_error=False)
    parser.accept(sequence[1])
    assert not parser.sources


def test_incomplete_stream_and_usage_limits_are_errors_not_empty_success():
    parser = cs.SearchEvents(lambda message: None)
    parser.accept(events()[0])
    with pytest.raises(ValueError, match="did not finish"):
        parser.finish()
    parser.accept(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "You've hit your usage limit",
        }
    )
    with pytest.raises(ValueError, match="usage limit"):
        parser.finish()


def test_environment_keeps_oauth_but_removes_api_keys_and_provider_overrides(monkeypatch):
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDECODE",
    ):
        monkeypatch.setenv(name, "must-not-be-used")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "subscription-token")
    env = cs.subscription_env()
    assert "must-not-be-used" not in env.values()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "subscription-token"


@pytest.mark.parametrize(
    "status",
    [
        {"loggedIn": False},
        {"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"},
        {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"},
    ],
)
def test_api_auth_or_missing_login_cannot_fall_back_to_another_provider(monkeypatch, status):
    monkeypatch.setattr(
        cs.subprocess,
        "run",
        Mock(return_value=SimpleNamespace(stdout=json.dumps(status), returncode=0)),
    )
    with pytest.raises(ValueError, match="subscription login"):
        cs.require_subscription({}, "/tmp")


def test_worker_has_only_web_tools_and_never_inherits_repo_settings_or_opens_chrome(monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(cs, "require_subscription", Mock())
    proc = MagicMock()
    proc.__enter__.return_value = proc
    proc.stdout = io.StringIO("\n".join(json.dumps(event) for event in events()))
    proc.poll.return_value = 0
    proc.wait.return_value = 0
    popen = Mock(return_value=proc)
    monkeypatch.setattr(cs.subprocess, "Popen", popen)
    result = cs.run_search("Find jobs", {"type": "object"}, lambda message: None)
    assert result["source_urls"] == {URL}
    args, kwargs = popen.call_args
    cmd = args[0]
    assert cmd[cmd.index("--tools") + 1] == "WebSearch,WebFetch"
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert "--no-chrome" in cmd and "--chrome" not in cmd
    assert "--safe-mode" in cmd and "--bare" not in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert kwargs["stdin"] == cs.subprocess.DEVNULL
    assert "appliedin-career-search-" in kwargs["cwd"]


def test_subscription_failure_never_launches_a_search_worker(monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(cs, "require_subscription", Mock(side_effect=ValueError("Sign in")))
    popen = Mock()
    monkeypatch.setattr(cs.subprocess, "Popen", popen)
    with pytest.raises(ValueError, match="Sign in"):
        cs.run_search("Find jobs", {}, lambda message: None)
    popen.assert_not_called()
