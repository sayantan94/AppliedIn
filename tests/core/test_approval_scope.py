"""A company approval must never enqueue another employer or an unseen new job."""
import json
from types import SimpleNamespace

import fakeredis
import pytest

import server
from core.models import Status
from core.storage.local import RedisTracking


@pytest.fixture
def approval(monkeypatch):
    r = fakeredis.FakeRedis(decode_responses=True)
    tracking = RedisTracking(r)
    for pk, company, status, reason in [
        ("acme#shown", "Acme", Status.TAILORED, "approval"),
        ("acme#hidden", "Acme", Status.TAILORED, "approval"),
        ("beta#shown", "Beta", Status.TAILORED, "approval"),
        ("acme#question", "Acme", Status.NEEDS_HUMAN, "unknown_field"),
        ("acme#sent", "Acme", Status.APPLIED, ""),
    ]:
        tracking.set_status(pk, status, company=company, gate_reason=reason)
    monkeypatch.setattr(server, "make_stores", lambda *a: SimpleNamespace(tracking=tracking))
    app = server.create_app()
    endpoint = next(route.endpoint for route in app.routes
                    if getattr(route, "path", "") == "/actions/approve-all")

    def queued(company):
        return [json.loads(raw)["pk"] for raw in r.lrange(f"applyq:co:{company}", 0, -1)]

    return endpoint, queued


def test_company_and_exact_ids_both_bound_the_approval(approval):
    approve, queued = approval
    result = approve({"company": "ACME", "pks": ["acme#shown", "beta#shown",
                                                "acme#question", "acme#sent"]})
    assert result["queued"] == 1
    assert queued("acme") == ["acme#shown"]
    assert queued("beta") == [], "even an explicit foreign ID cannot widen company approval"


def test_empty_selection_approves_nothing(approval):
    approve, queued = approval
    assert approve({"company": "__all__", "pks": []})["queued"] == 0
    assert queued("acme") == queued("beta") == []


def test_explicit_global_selection_still_approves_only_the_confirmed_ids(approval):
    approve, queued = approval
    assert approve({"company": "__all__", "pks": ["beta#shown"]})["queued"] == 1
    assert queued("acme") == []
    assert queued("beta") == ["beta#shown"]


@pytest.mark.parametrize("body", [{}, {"company": ""}, {"company": "Acme", "pks": None},
                                 {"company": "Acme", "pks": "acme#shown"}])
def test_missing_scope_or_malformed_ids_never_mean_global_approval(approval, body):
    approve, queued = approval
    assert not approve(body)["ok"]
    assert queued("acme") == queued("beta") == []


def test_older_company_scoped_clients_still_stay_within_that_company(approval):
    approve, queued = approval
    assert approve({"company": "Acme"})["queued"] == 2
    assert set(queued("acme")) == {"acme#shown", "acme#hidden"}
    assert queued("beta") == []
