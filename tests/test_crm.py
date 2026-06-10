import pytest
from pipeline.crm import CRM, metrics, digest_report
from test_replies import sent, event


def engaged(setup, seed):
    app, _, contact, _ = sent(setup, seed)
    app.ingest(event(contact, "I am interested"))
    app.classify("incoming-1")
    return app, contact


def test_proposal_requires_scope_approval_before_commitment(setup, seed):
    app, contact = engaged(setup, seed)
    crm = CRM(app)
    proposal = crm.proposal(contact["id"])
    assert proposal["hours"] == 2
    assert proposal["approval_required"]
    with pytest.raises(ValueError, match="approval"):
        crm.win(contact["id"], free=True, approved=False)
    project = crm.win(contact["id"], free=True, approved=True)
    assert project["free"]
    crm.complete(
        project["id"],
        hours=2,
        revenue=0,
        cost=10,
        customer_value="Staff report less retyping",
        verified_results="Measured 15 minutes saved per document on 5 samples",
    )
    assert app.store.one("select state from contacts")["state"] == "PROJECT_COMPLETED"
    assert "15 minutes" in crm.case_study(project["id"])["verified_results"]


def test_metrics_distinguish_positive_replies_and_revenue(setup, seed):
    app, contact = engaged(setup, seed)
    result = metrics(app, "automation")
    assert result["contacted"] == 1
    assert result["positive_replies"] == 1
    assert result["replies"] == 1
    assert result["revenue"] == 0
    assert result["revenue_per_qualified_prospect"] == 0
    assert digest_report(app)["highest_leverage_action"]


def test_free_project_limit_and_explicit_state_transitions(setup, seed):
    app, contact = engaged(setup, seed)
    app.configure(app.campaign("automation").model_copy(update={"max_free_projects": 0}))
    crm = CRM(app)
    crm.proposal(contact["id"])
    with pytest.raises(ValueError, match="free project"):
        crm.win(contact["id"], free=True, approved=True)
    with pytest.raises(ValueError, match="Invalid transition"):
        app.transition(contact["id"], "PROJECT_COMPLETED")


def test_experiments_assign_once_across_campaigns(setup, seed):
    app, _, _, campaign = setup
    campaign.experiment_variants = ["short", "assessment"]
    app.configure(campaign)
    contact = app.prepare(campaign.id, seed)
    assert contact["variant"] in campaign.experiment_variants
    app.prepare(campaign.id, seed)
    assert app.store.one("select count(*) n from experiments")["n"] == 1
