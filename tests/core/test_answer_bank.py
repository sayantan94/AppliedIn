from core.models import AnswerScope
from core.storage.answer_bank import AnswerBank

from .conftest import make_answer_bank_table


def test_global_fact_reused_across_companies(aws):
    make_answer_bank_table()
    bank = AnswerBank("answer_bank")
    bank.put("Do you require sponsorship?", "Yes, H-1B", AnswerScope.GLOBAL)
    # different company, synonym-spaced/cased label -> still resolves
    assert bank.lookup("do you require sponsorship", "Stripe") == "Yes, H-1B"
    assert bank.lookup("Do You Require Sponsorship?", "Acme") == "Yes, H-1B"


def test_company_scope_shadows_global_only_for_that_company(aws):
    make_answer_bank_table()
    bank = AnswerBank("answer_bank")
    bank.put("Why us?", "generic", AnswerScope.GLOBAL)
    bank.put("Why us?", "I love Stripe payments", AnswerScope.COMPANY, company="Stripe")
    assert bank.lookup("why us", "Stripe") == "I love Stripe payments"
    assert bank.lookup("why us", "Acme") == "generic"


def test_seed_global(aws):
    make_answer_bank_table()
    bank = AnswerBank("answer_bank")
    bank.seed_global({"Notice period": "30 days", "Salary": "$200k"})
    assert bank.lookup("notice period?", "Any") == "30 days"
