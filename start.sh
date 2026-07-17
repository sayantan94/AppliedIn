#!/usr/bin/env bash
# AppliedIn — local start. Runs setup.sh the first time (or when deps change),
# then launches the daemon (dashboard + discovery + apply).
set -euo pipefail
cd "$(dirname "$0")"

say()  { printf "\033[1;32m▸\033[0m %s\n" "$1"; }
warn() { printf "\033[1;33m⚠\033[0m  %s\n" "$1"; }
export PATH="$HOME/.local/bin:$PATH"

# --- setup only when needed (env-per-version stamp) ---------------------------
VER_HASH="$(cat pyproject.toml uv.lock 2>/dev/null | shasum | cut -d' ' -f1)"
if [ ! -f .venv/.appliedin-env ] || [ "$(cat .venv/.appliedin-env 2>/dev/null || true)" != "$VER_HASH" ]; then
  say "first run / deps changed — running setup…"
  ./setup.sh
else
  say "environment up to date — reusing .venv"
fi

# --- ensure Redis is up -------------------------------------------------------
redis-cli ping >/dev/null 2>&1 || { redis-server --daemonize yes >/dev/null && say "started redis"; }

# --- preflight ----------------------------------------------------------------
[ -f .env ] || { warn "no .env — run ./setup.sh"; exit 1; }
grep -q '^ANTHROPIC_API_KEY=.\+' .env || { warn "paste your key into .env (ANTHROPIC_API_KEY=...) and re-run"; exit 1; }
[ -f resume/base.tex ] || warn "no resume/base.tex — save your résumé there (tailoring needs it)"

# --- run ----------------------------------------------------------------------
export APPLIEDIN_MODE=local
say "dashboard → http://127.0.0.1:8787   (Ctrl-C to stop)"
exec uv run python -m daemon
