#!/usr/bin/env bash
# AppliedIn — one-time setup. Installs everything needed to run locally: uv, the
# Python env, Redis (queues + runtime flags) and Tectonic (renders the résumé
# PDF). Idempotent — safe to re-run; skips what's already there.
#
# Applications are submitted through the Claude Code CLI driving your own Chrome,
# so there is no headless browser to install. That needs `claude` on PATH and a
# Claude subscription; this script checks for it and tells you if it is missing.
set -euo pipefail
cd "$(dirname "$0")"

say()  { printf "\033[1;32m▸\033[0m %s\n" "$1"; }
warn() { printf "\033[1;33m⚠\033[0m  %s\n" "$1"; }

# --- uv -----------------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  say "installing uv…"; curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

# --- Python env + deps --------------------------------------------------------
say "installing Python 3.12 + dependencies…"
uv python install 3.12
uv sync

# --- system tools (macOS via brew) -------------------------------------------
if command -v brew >/dev/null 2>&1; then
  command -v redis-server >/dev/null 2>&1 || { say "installing redis…";    brew install redis; }
  command -v tectonic     >/dev/null 2>&1 || { say "installing tectonic…"; brew install tectonic; }
else
  warn "Homebrew not found — install redis and tectonic manually."
fi

# --- Claude Code CLI (this is what submits applications) ----------------------
if command -v claude >/dev/null 2>&1; then
  say "found the Claude Code CLI"
else
  warn "no 'claude' on PATH. Discovery and tailoring still work, but applying"
  warn "needs it: https://claude.com/claude-code  (a Claude subscription, not an"
  warn "API key — it refuses API-key auth for this)"
fi

# --- .env ---------------------------------------------------------------------
[ -f .env ] || { cp .env.example .env 2>/dev/null && say "created .env from template"; }

# --- stamp the env version (appliedin reuses it until deps change) ------------
mkdir -p .venv
cat pyproject.toml uv.lock 2>/dev/null | shasum | cut -d' ' -f1 > .venv/.appliedin-env

say "setup complete."

# Report only what is ACTUALLY missing. This used to print the same two reminders
# every run, so someone who had already done both was told to do them again, and
# the message stopped being read at exactly the point it started mattering.
missing=0
grep -q '^OPENAI_API_KEY=.\+' .env 2>/dev/null || { warn "put OPENAI_API_KEY in .env"; missing=1; }
[ -f resume/base.tex ] || { warn "save your résumé to resume/base.tex (LaTeX)"; missing=1; }
[ "$missing" -eq 0 ] && say "ready — run ./appliedin start"
