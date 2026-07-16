"""Scripted Greenhouse fill against a fake page double.

The fake implements exactly the minimal Page surface documented in
``engines/scripted/base.py``: query_selector_all / inner_text /
get_attribute / fill.
"""

from __future__ import annotations

from appliedin_core.models import JobRecord
from appliedin_core.storage.answer_bank import AnswerBank
from appliedin_worker.confidence import resolve_field
from appliedin_worker.engines import pick_engine
from appliedin_worker.engines.scripted import SCRIPTED_ENGINES
from appliedin_worker.engines.scripted.greenhouse import GreenhouseFillEngine


class FakeLabel:
    def __init__(self, text, target):
        self._text = text
        self._target = target

    async def inner_text(self):
        return self._text

    async def get_attribute(self, name):
        return self._target if name == "for" else None


class FakeFormPage:
    def __init__(self, labels):
        self._labels = [FakeLabel(t, f) for t, f in labels]
        self.filled = {}

    async def query_selector_all(self, selector):
        return self._labels if selector == "label" else []

    async def fill(self, selector, value):
        self.filled[selector] = value


def _job():
    return JobRecord(
        company="Acme", job_id="1", title="SWE", jd_url="u",
        jd_text="x", location="R", ats="greenhouse",
    )


async def test_greenhouse_fill_resolves_and_flags(answer_bank_table):
    bank = AnswerBank(answer_bank_table)
    bank.seed_global({"first name": "Sayantan", "work authorization": "Yes"})
    page = FakeFormPage(
        [
            ("First Name", "first_name"),
            ("Are you legally authorized to work in the United States?", "auth"),
            ("Why do you want to work at Acme?", "essay"),
        ]
    )

    engine = GreenhouseFillEngine()
    result = await engine.fill(
        page, _job(), lambda label: resolve_field(label, "greenhouse", bank, "acme")
    )

    # Known fields filled from the bank, via the documented selectors.
    assert page.filled == {"#first_name": "Sayantan", "#auth": "Yes"}
    assert result.fields == {
        "First Name": "Sayantan",
        "Are you legally authorized to work in the United States?": "Yes",
    }
    # The essay gates; it is never filled.
    assert result.low_confidence_labels == ["Why do you want to work at Acme?"]
    assert "#essay" not in page.filled
    # Snapshot captures the form structure for the approval-resume diff.
    assert result.form_snapshot["ats"] == "greenhouse"
    assert {f["label"] for f in result.form_snapshot["fields"]} == {
        "First Name",
        "Are you legally authorized to work in the United States?",
        "Why do you want to work at Acme?",
    }


def test_pick_engine_prefers_scripted_and_falls_back_to_agentic():
    from appliedin_worker.engines.agentic import AgenticFillEngine

    assert isinstance(pick_engine("greenhouse"), GreenhouseFillEngine)
    assert isinstance(pick_engine("some-custom-portal"), AgenticFillEngine)
    assert set(SCRIPTED_ENGINES) == {"greenhouse", "lever"}
