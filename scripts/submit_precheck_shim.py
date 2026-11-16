"""Copyable compatibility entrypoint for the ignored legacy precheck path.

After merge, the operator first prepares the untracked precheck-data.json,
verifies the CLI compatibility in a disposable environment, and only then
copies this file to .local/operator/precheck.py while all application work is
idle. This tracked template must never be installed by CI or this PR.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _source_directory(script: Path) -> Path:
    # Installed at <checkout>/.local/operator/precheck.py, the module is in
    # <checkout>/src. The tracked copy in <checkout>/scripts also works.
    # Do not depend on CWD or a caller-provided checkout/src PYTHONPATH.
    for directory in script.parents:
        source = directory / "src"
        if (source / "tools" / "submit_precheck.py").is_file():
            return source
    raise RuntimeError("canonical submit precheck not found beside installed shim")


def main(argv: list[str] | None = None) -> int:
    script = Path(__file__).resolve()
    try:
        source = _source_directory(script)
    except RuntimeError:
        print("STOP: precheck unavailable", file=sys.stderr)
        return 2
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    from tools.submit_precheck import main as precheck_main

    return precheck_main(argv, legacy_dir=script.parent)


if __name__ == "__main__":
    raise SystemExit(main())
