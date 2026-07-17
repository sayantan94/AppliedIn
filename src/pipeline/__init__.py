"""The per-application agentic pipeline.

For each job the discovery queue produces, this runs a SEQUENTIAL chain of
agent steps. At any step that gets stuck (needs data it doesn't have, hits a
CAPTCHA, or can't fill a field confidently) it raises a GATE: it persists
where it stopped and asks the human. When the human answers from the website,
`resume()` picks up from exactly that step. This is the human-in-the-loop.
"""

__version__ = "0.1.0"
