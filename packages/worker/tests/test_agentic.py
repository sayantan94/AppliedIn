"""Agentic engine determinism contract: the model proposes LABELS only —
values and confidence always come from resolve_field, so a free-form field is
low-confidence no matter what the model claims."""

from __future__ import annotations

from appliedin_core.models import JobRecord
from appliedin_core.storage.answer_bank import AnswerBank
from appliedin_worker.confidence import resolve_field
from appliedin_worker.engines.agentic import AgenticFillEngine, parse_proposed_fields


class FakePage:
    def __init__(self):
        self.filled = {}

    async def fill(self, selector, value):
        self.filled[selector] = value


def _job():
    return JobRecord(
        company="Acme", job_id="1", title="SWE", jd_url="u",
        jd_text="x", location="R", ats="custom",
    )


async def test_free_form_field_is_low_confidence_regardless_of_the_model(answer_bank_table):
    bank = AnswerBank(answer_bank_table)
    bank.seed_global({"work authorization": "Yes"})

    async def overconfident_model(page, job):
        # The "model" proposes an essay field (and would happily answer it) —
        # plus a real fact field. Only labels reach the confidence gate.
        return [
            {"label": "Describe a time when you failed", "selector": "#q1"},
            {"label": "Work authorization", "selector": "#auth"},
        ]

    engine = AgenticFillEngine(discover=overconfident_model)
    page = FakePage()
    result = await engine.fill(
        page, _job(), lambda label: resolve_field(label, "custom", bank, "acme")
    )

    # Deterministic gate: essay gates, fact fills from the bank.
    assert result.low_confidence_labels == ["Describe a time when you failed"]
    assert result.fields == {"Work authorization": "Yes"}
    assert page.filled == {"#auth": "Yes"}  # value came from the bank, not the model
    assert result.form_snapshot["engine"] == "agentic"


async def test_engine_needs_no_strands_when_discoverer_is_injected(answer_bank_table):
    bank = AnswerBank(answer_bank_table)

    async def discover(page, job):
        return [{"label": "Anything", "selector": "#x"}]

    result = await AgenticFillEngine(discover=discover).fill(
        FakePage(), _job(), lambda label: resolve_field(label, "custom", bank, "acme")
    )
    assert result.low_confidence_labels == ["Anything"]


def test_parse_proposed_fields_is_defensive():
    reply = 'Sure! Here you go:\n[{"label": "A", "selector": "#a"}, "junk", {"label": "B"}]'
    assert parse_proposed_fields(reply) == [
        {"label": "A", "selector": "#a"},
        {"label": "B"},
    ]
    assert parse_proposed_fields("no json here") == []
    assert parse_proposed_fields("[{broken") == []
