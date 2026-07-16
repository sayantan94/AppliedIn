"""One-time seed of the global facts into the DynamoDB answer_bank.

There is no facts.yaml at runtime — DDB is the single source of truth. This
script loads a local, git-ignored ``facts.seed.yaml`` (copy the example) and
writes each entry to the global scope. After seeding, facts are updated only
via WhatsApp (gate replies or the /fact command).

Usage:
    APPLIEDIN_ANSWER_BANK_TABLE=answer_bank uv run python scripts/seed_facts.py facts.seed.yaml
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from appliedin_core.config import get_settings
from appliedin_core.storage.answer_bank import AnswerBank


def main(seed_path: str) -> None:
    entries = yaml.safe_load(Path(seed_path).read_text()) or {}
    if not isinstance(entries, dict) or not entries:
        raise SystemExit(f"{seed_path} must be a non-empty mapping of question: answer")

    settings = get_settings()
    bank = AnswerBank(settings.answer_bank_table, region=settings.aws_region)
    bank.seed_global(entries)
    print(f"Seeded {len(entries)} global facts into {settings.answer_bank_table}")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "facts.seed.yaml"
    main(path)
