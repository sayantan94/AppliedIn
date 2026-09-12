"""Install the pinned HTTP providers without running upstream setup or agents."""

from __future__ import annotations

import fcntl
import os
import subprocess
import tempfile
from pathlib import Path

REVISION = "da8c6f9193ac3d7a48a583f815b7d0feab742b81"
UPSTREAM = "https://github.com/career-ops-hq/career-ops.git"
ROOT = Path(__file__).resolve().parents[2]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    ).stdout.strip()


def _validate(repo: Path) -> None:
    if not (repo / "templates/portals.example.yml").is_file():
        raise ValueError("Career Ops company catalog is missing.")
    # Loading every provider catches missing files and incompatible Node versions
    # before startup. An empty catalog never fetches jobs or calls a model.
    result = subprocess.run(
        ["node", str(ROOT / "scripts/integrations/career-ops.mjs"), str(repo)],
        input='{"catalog":true,"entries":[]}',
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    if result.stdout.strip() != "[]":
        raise ValueError("Career Ops provider check returned an unexpected result.")


def ensure_checkout(local_dir: Path) -> bool:
    """Return whether installation changed. Healthy installations need no network."""
    parent = local_dir.resolve() / "integrations"
    parent.mkdir(parents=True, exist_ok=True)
    repo = parent / "career-ops"
    # Concurrent starts must never checkout files underneath another installer.
    with (parent / ".career-ops-install.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(
                "Career Ops setup is already running. Retry startup when it finishes."
            ) from exc
        if repo.exists():
            if _git(repo, "rev-parse", "--show-toplevel") != str(repo.resolve()):
                raise ValueError(
                    f"{repo} is not a Career Ops checkout; existing files were preserved."
                )
            if _git(repo, "status", "--porcelain"):
                raise ValueError(
                    f"Career Ops has local edits at {repo}; save or move them before retrying."
                )
            changed = _git(repo, "rev-parse", "HEAD") != REVISION
            if changed:
                print("▸ restoring the supported Career Ops version…", flush=True)
                _git(repo, "fetch", "--depth=1", UPSTREAM, REVISION)
                _git(repo, "checkout", "--detach", REVISION)
            _validate(repo)
            return changed

        print("▸ installing Career Ops public job providers…", flush=True)
        # Failed downloads leave no half-installed destination for the next start.
        with tempfile.TemporaryDirectory(prefix=".career-ops-", dir=parent) as staging:
            candidate = Path(staging) / "checkout"
            candidate.mkdir()
            _git(candidate, "init", "--quiet")
            _git(candidate, "fetch", "--depth=1", UPSTREAM, REVISION)
            _git(candidate, "checkout", "--detach", REVISION)
            _validate(candidate)
            candidate.rename(repo)
        return True


def main() -> int:
    from dotenv import load_dotenv

    # Match the daemon's .env / environment precedence without importing the
    # model clients in core.config on every startup.
    load_dotenv(ROOT / ".env")
    try:
        changed = ensure_checkout(Path(os.environ.get("APPLIEDIN_LOCAL_DIR", ".local")))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"Career Ops setup failed: {detail}\nRetry with ./appliedin start.", flush=True)
        return 1
    print(
        "▸ Career Ops installed and checked"
        if changed
        else "▸ Career Ops ready (already installed)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
