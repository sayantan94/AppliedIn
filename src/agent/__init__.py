"""The AppliedIn agent — the ADK pipeline (score → tailor → apply).

`adk web src/agent` opens a local dev UI to drive and trace it. Models run on
Claude (Anthropic local / Bedrock cloud) via LiteLLM — no Google cloud required.
"""

from . import graph

root_agent = graph.root_agent

__all__ = ["root_agent", "graph"]
