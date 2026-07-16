"""The ``answer_bank`` DynamoDB table — single source of truth for facts + answers.

There is no facts.yaml. Two scopes share one table:
  pk = "global"            -> facts reusable on every portal (visa, EEO, salary)
  pk = "company#<name>"    -> company-specific free text ("why us?")
Lookup prefers the company scope, then falls back to global. Every entry is
human-approved (seed script, gate reply, or /fact), so a hit is high-confidence.
"""

from __future__ import annotations

import boto3

from ..ids import make_pk, normalize_label
from ..models import AnswerScope

_GLOBAL_PK = "global"


def _company_pk(company: str) -> str:
    return f"company#{company.strip().lower()}"


class AnswerBank:
    def __init__(self, table_name: str, *, region: str | None = None) -> None:
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def lookup(self, question: str, company: str) -> str | None:
        """Company scope shadows global; returns the approved answer or None."""
        label = normalize_label(question)
        for pk in (_company_pk(company), _GLOBAL_PK):
            item = self._table.get_item(Key={"pk": pk, "sk": label}).get("Item")
            if item:
                return item["answer"]
        return None

    def put(
        self,
        question: str,
        answer: str,
        scope: AnswerScope,
        *,
        company: str | None = None,
        source: str = "whatsapp_reply",
        approved_at: str = "",
    ) -> None:
        if scope is AnswerScope.COMPANY and not company:
            raise ValueError("company is required for COMPANY scope")
        pk = _company_pk(company) if scope is AnswerScope.COMPANY else _GLOBAL_PK
        self._table.put_item(
            Item={
                "pk": pk,
                "sk": normalize_label(question),
                "question_raw": question,
                "answer": answer,
                "source": source,
                "approved_at": approved_at,
                "use_count": 0,
            }
        )

    def seed_global(self, entries: dict[str, str], *, source: str = "seed") -> None:
        """Bulk-load the initial global facts (one-time setup)."""
        with self._table.batch_writer() as batch:
            for question, answer in entries.items():
                batch.put_item(
                    Item={
                        "pk": _GLOBAL_PK,
                        "sk": normalize_label(question),
                        "question_raw": question,
                        "answer": answer,
                        "source": source,
                        "use_count": 0,
                    }
                )


__all__ = ["AnswerBank", "make_pk"]
