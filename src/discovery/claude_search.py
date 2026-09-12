"""Subscription-backed, read-only Claude Code worker for Career Ops searches."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from urllib.parse import urlsplit

log = logging.getLogger(__name__)
TIMEOUT = 240


def subscription_env() -> dict[str, str]:
    # Claude otherwise gives API keys priority over the user's subscription.
    # Keep its OAuth token/keychain login, while excluding paid-provider routing.
    blocked = {"CLAUDECODE", "CLAUDE_CODE_BARE", "CLAUDE_CODE_API_KEY"}
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("ANTHROPIC_", "OPENAI_", "CLAUDE_CODE_USE_")) and key not in blocked
    }


def require_subscription(env: dict, cwd: str) -> None:
    result = subprocess.run(
        ["claude", "--setting-sources", "", "auth", "status", "--json"],
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )
    try:
        status = json.loads(result.stdout)
    except ValueError as exc:
        raise ValueError(
            "Could not check Claude login. Run claude auth status, then retry."
        ) from exc
    if (
        result.returncode
        or not status.get("loggedIn")
        or status.get("authMethod") != "claude.ai"
        or status.get("apiProvider") != "firstParty"
    ):
        raise ValueError(
            "Career Ops search needs your Claude subscription login. "
            "Run claude auth login and sign in with your subscribed account, then retry."
        )


def _urls(value) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_urls(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_urls(v) for v in value)) if value else set()
    if isinstance(value, str):
        return set(re.findall(r'https://[^\s<>"\\\]\)]+', value))
    return set()


class SearchEvents:
    """Ground leads in actual tool results, never the model's proposed URLs."""

    def __init__(self, progress):
        self.progress = progress
        self.calls = {}
        self.sources = set()
        self.queries = []
        self.completed_searches = set()
        self.result = None
        self.comparing = False

    def accept(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    name, data = block.get("name"), block.get("input") or {}
                    if name not in ("WebSearch", "WebFetch") or block["id"] in self.calls:
                        continue
                    self.calls[block["id"]] = (name, data)
                    if name == "WebSearch":
                        query = str(data.get("query") or "")
                        self.queries.append(query)
                        self.progress(f"Searching: {query}")
                    elif data.get("url"):
                        self.progress(f"Reading posting on {urlsplit(data['url']).hostname}…")
                elif block.get("type") == "text" and self.calls and not self.comparing:
                    self.comparing = True
                    self.progress("Comparing roles with your preferences…")
        elif kind == "user":
            for block in event.get("message", {}).get("content", []):
                call = self.calls.get(block.get("tool_use_id"))
                if block.get("type") != "tool_result" or not call or block.get("is_error"):
                    continue
                self.sources.update(_urls(block.get("content")))
                self.sources.update(_urls(event.get("tool_use_result")))
                if call[0] == "WebSearch":
                    self.completed_searches.add(block["tool_use_id"])
                if call[0] == "WebFetch" and call[1].get("url"):
                    self.sources.add(call[1]["url"])
                self.progress(
                    "Search results received" if call[0] == "WebSearch" else "Posting read"
                )
        elif kind == "result":
            self.result = event

    def finish(self) -> dict:
        result = self.result or {}
        if result.get("is_error") or result.get("subtype") != "success":
            detail = " ".join(str(e) for e in result.get("errors", [])) or str(
                result.get("result") or ""
            )
            log.warning("Career Ops Claude search did not finish: %s", detail[:1500])
            if any(word in detail.lower() for word in ("limit", "capacity", "overloaded")):
                raise ValueError(
                    "Claude's usage limit or capacity stopped this search. Retry when available."
                )
            raise ValueError(
                "Claude search did not finish. Retry; no incomplete results were imported."
            )
        parsed = result.get("structured_output")
        if not isinstance(parsed, dict) or not isinstance(parsed.get("jobs"), list):
            raise ValueError(
                "Claude did not return a complete job list. Update Claude Code and retry."
            )
        if not self.queries or not self.completed_searches:
            raise ValueError(
                "Claude did not run a web search. Retry; no unverified results were imported."
            )
        return {"parsed": parsed, "source_urls": self.sources, "queries": self.queries}


def run_search(prompt: str, schema: dict, progress) -> dict:
    if not shutil.which("claude"):
        raise ValueError(
            "Install Claude Code and sign in with your Claude subscription to search jobs."
        )
    env = subscription_env()
    with tempfile.TemporaryDirectory(prefix="appliedin-career-search-") as cwd:
        progress("Checking your Claude subscription login…")
        require_subscription(env, cwd)
        # A scratch directory and safe mode keep repo memories, hooks and plugins
        # out of discovery. --bare is deliberately absent: it disables OAuth login.
        cmd = [
            "claude",
            "--safe-mode",
            "--setting-sources",
            "",
            "--no-chrome",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--no-session-persistence",
            "--permission-mode",
            "dontAsk",
            "--tools",
            "WebSearch,WebFetch",
            "--allowedTools",
            "WebSearch,WebFetch",
            "--output-format",
            "stream-json",
            "--verbose",
            "--json-schema",
            json.dumps(schema),
            "-p",
            prompt,
        ]
        progress("Claude is planning searches using your job preferences…")
        events = SearchEvents(progress)
        timed_out = threading.Event()
        with (
            tempfile.TemporaryFile() as stderr,
            subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
            ) as proc,
        ):

            def expire():
                if proc.poll() is None:
                    timed_out.set()
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass

            timer = threading.Timer(TIMEOUT, expire)
            timer.daemon = True
            timer.start()
            try:
                for line in proc.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        events.accept(event)
                code = proc.wait()
            finally:
                timer.cancel()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            if timed_out.is_set():
                raise ValueError(
                    "Claude search timed out. Retry; no incomplete results were imported."
                )
            if code and not events.result:
                stderr.seek(0)
                detail = stderr.read().decode(errors="replace")[-1500:]
                log.warning("Career Ops Claude startup failed: %s", detail)
                raise ValueError(
                    "Claude search could not start. Check your Claude login and update Claude Code."
                )
            if code:
                raise ValueError(
                    "Claude search exited before completing. Check your Claude usage and retry."
                )
        return events.finish()
