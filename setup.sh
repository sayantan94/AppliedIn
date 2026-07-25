#!/usr/bin/env bash
# AppliedIn — one-time setup. Installs EVERYTHING needed to run locally:
# uv, the Python env (with the browser runtime extra), the Playwright
# browser, Redis, Tectonic, Node, and the Playwright MCP server. Idempotent —
# safe to re-run; skips what's already there.
set -euo pipefail
cd "$(dirname "$0")"

say()  { printf "\033[1;32m▸\033[0m %s\n" "$1"; }
warn() { printf "\033[1;33m⚠\033[0m  %s\n" "$1"; }

# --- uv -----------------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  say "installing uv…"; curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

# --- Python env + deps (incl. runtime extra: playwright) ----------------------
say "installing Python 3.12 + dependencies…"
uv python install 3.12
uv sync --extra runtime

# --- Playwright browser (crawling JS sites + applying) ------------------------
say "installing the Playwright browser (chromium)…"
uv run playwright install chromium

# --- system tools (macOS via brew) -------------------------------------------
if command -v brew >/dev/null 2>&1; then
  command -v redis-server >/dev/null 2>&1 || { say "installing redis…";    brew install redis; }
  command -v tectonic     >/dev/null 2>&1 || { say "installing tectonic…"; brew install tectonic; }
  command -v node         >/dev/null 2>&1 || { say "installing node…";     brew install node; }
else
  warn "Homebrew not found — install redis, tectonic, and node manually."
fi

# --- Playwright MCP server (the applier's browser) ----------------------------
if command -v npx >/dev/null 2>&1; then
  say "prefetching the Playwright MCP server…"
  npx -y @playwright/mcp@latest --help >/dev/null 2>&1 || true
fi

# --- .env ---------------------------------------------------------------------
[ -f .env ] || { cp .env.example .env 2>/dev/null && say "created .env from template"; }

# --- stamp the env version (start.sh reuses it until deps change) -------------
mkdir -p .venv
cat pyproject.toml uv.lock 2>/dev/null | shasum | cut -d' ' -f1 > .venv/.appliedin-env

say "setup complete."
warn "before ./start.sh:  (1) put ANTHROPIC_API_KEY in .env   (2) save your résumé to resume/base.tex"
