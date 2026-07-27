# CLAUDE.md

Read **[AGENTS.md](AGENTS.md)** before changing anything. It is the guide for
working on this repo: layout, how to add a job preference or a per site playbook,
how to debug the browser agent, and the invariants that must stay enforced in
code.

Everything in this file is a pointer to that one. If the two ever disagree,
AGENTS.md is right.

**If you are asked to set this repo up and start the server**, follow
[AGENTS.md → Setup](AGENTS.md#setup-set-this-repo-up-and-start-the-server) exactly.
It is scripted end to end through a single entry point, `./appliedin`. Ask the
human for the three things only they have (an OpenAI key, their résumé at
`resume/base.tex`, and a company or two to watch) rather than inventing values,
and verify each step before moving to the next.

## The rule that outranks the rest

An application is sent under a real person's name and cannot be recalled. A
missed application costs an opportunity; a wrong one costs credibility. When a
change trades safety for throughput, it is the wrong change.

## Before you start

- **Restart the daemon after any `src/` change.** There is no hot reload, and the
  browser caches `web/app.js` and `web/styles.css`, so a UI change needs a hard
  reload as well. Testing stale code has wasted more time here than any bug.
- **Run the tests**: `.venv/bin/python -m pytest -q`. They are offline by design;
  never add one that reaches the network or drives a browser.
- **Diagnose with evidence, not theory.** The worst bug in this repo's history was
  guessed at three times and solved once, by reading the browser session's own
  report instead of its one line verdict. AGENTS.md has the drill.

## Do not

- Move a guard from code into a prompt. The invariants in AGENTS.md hold because
  the tool layer enforces them regardless of what the model emits.
- Commit `.local/`, `resume/base.tex` or anything under `output/`. They hold real
  personal data. `config/watchlist.yaml` and `config/preferences.yaml` are
  tracked, so treat them as public.
- Add a preference field to the interface without wiring it into
  `discovery/relevance.py`. A setting nothing reads is a lie.
