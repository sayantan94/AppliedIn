# Sourced by both setup and start so the daemon inherits any installed tool paths.
ensure_career_ops() {
  if ! command -v git >/dev/null 2>&1; then
    command -v brew >/dev/null 2>&1 || {
      warn "Career Ops needs Git. Install Git, then run ./appliedin start again."; return 1;
    }
    say "installing Git for Career Ops…"
    brew install git || return 1
    export PATH="$(brew --prefix git)/bin:$PATH"
  fi

  if ! command -v node >/dev/null 2>&1 || ! node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 18 ? 0 : 1)' >/dev/null 2>&1; then
    command -v brew >/dev/null 2>&1 || {
      warn "Career Ops needs Node.js 18+. Install Node.js, then run ./appliedin start again."; return 1;
    }
    say "installing Node.js for Career Ops…"
    brew install node || return 1
    export PATH="$(brew --prefix node)/bin:$PATH"
  fi

  node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 18 ? 0 : 1)' >/dev/null 2>&1 || {
    warn "Career Ops needs Node.js 18+. Update Node.js on PATH, then retry startup."; return 1;
  }

  .venv/bin/python -m discovery.career_ops_setup
}
