"""Legacy CLI shim for the free-text checker (template; tracked copy).

The operator installs this file's content, unchanged, at the ignored legacy path
`.local/operator/freetext_check.py` after the change that added
`src/tools/freetext_check.py` is merged. It holds no rules: it finds the checkout
that owns it, puts that checkout's `src/` on the import path and forwards to the one
canonical checker, so the legacy invocation keeps working from any directory:

  env PYTHONPATH=src .venv/bin/python .local/operator/freetext_check.py '<pk>' /tmp/answer.txt

The module also re-exports the checker's names (`tells`, `check`, `TRANSPARENCY`, ...)
for callers that load the legacy file as a module.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _checkout_src() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "src" / "tools" / "freetext_check.py").is_file():
            return parent / "src"
    raise SystemExit("freetext_check shim: no checkout with src/tools/freetext_check.py "
                     f"above {here}")


_SRC = str(_checkout_src())
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from tools import freetext_check as _canonical  # noqa: E402

ROOT = _canonical.ROOT
COMMON = _canonical.COMMON
TRANSPARENCY = _canonical.TRANSPARENCY
_TELL_PATTERNS = _canonical._TELL_PATTERNS
tells = _canonical.tells
check = _canonical.check
_sources = _canonical._sources
main = _canonical.main

if __name__ == "__main__":
    sys.exit(main())
