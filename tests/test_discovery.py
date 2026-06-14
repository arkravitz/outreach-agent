import pytest
from conftest import SKILL_IS_STUB
from pipeline.models import Campaign
from pipeline.writing import WritingError


@pytest.mark.skipif(SKILL_IS_STUB, reason="requires the installed no-ai-slop skill")
def test_qualifies_and_queues_with_actual_editor_skill(setup, seed):
    app, model, _, _ = setup
    result = app.prepare("automation", seed)
    assert result["state"] == "READY_FOR_OUTREACH"
    message = app.store.one("select * from messages where contact_id=?", (result["id"],))
    assert message["status"] == "REVIEW"
    assert message["skill_hash"]
    edit = next(p for t, p in model.calls if t == "edit")
    assert "Protect the specific fact" in edit["skill"]
    assert "Final read" in edit["evaluation"]
    assert 'Let me know if you want me to stop contacting you.' in message["body"]
    assert app.store.one('select count(*) n from executions where status="DONE"')["n"] >= 3


def test_unqualified_rejected_without_drafting(setup, seed):
    app, model, _, _ = setup
    seed.evidence[0].text = "We sell shoes."
    seed.evidence[0].quote = "We sell shoes."
    result = app.prepare("automation", seed)
    assert result["state"] == "REJECTED"
    assert not any(t == "write" for t, _ in model.calls)


def test_cross_campaign_company_and_contact_dedupe(setup, seed):
    app, model, _, campaign = setup
    first = app.prepare("automation", seed)
    other = campaign.model_copy(update={"id": "engineering"})
    app.configure(other)
    second = app.prepare("engineering", seed)
    assert second["id"] == first["id"]
    assert app.store.one("select count(*) n from messages")["n"] == 1
    assert app.store.one("select count(*) n from companies")["n"] == 1


def test_unsupported_claim_rejected_after_editor(setup, seed):
    app, model, _, _ = setup
    model.bad = True
    with pytest.raises(WritingError, match="ground"):
        app.prepare("automation", seed)
    assert app.store.one("select count(*) n from messages")["n"] == 0


def test_missing_skill_fails_closed(setup, seed, tmp_path):
    app, _, _, _ = setup
    app.writer.skill_dir = tmp_path
    with pytest.raises(WritingError, match="no-ai-slop"):
        app.prepare("automation", seed)


def test_invalid_sender_and_campaign_limits_rejected():
    with pytest.raises(ValueError):
        Campaign(id="x", name="x", keywords=[], offer="unlimited", offer_hours=-1)


def test_evidence_quote_must_exist_in_source(setup, seed):
    app, _, _, _ = setup
    seed.evidence[0].quote = "They waste 50 hours weekly."
    with pytest.raises(ValueError, match="quote"):
        app.prepare("automation", seed)


def test_research_input_is_untrusted_in_model_instructions(setup, seed):
    app, model, _, _ = setup
    app.prepare("automation", seed)
    assert all(p.get("untrusted_data") for t, p in model.calls if t in {"write", "ground", "qualify"})


def test_redraft_after_configuration_change_replaces_stale_approval(setup, seed):
    app, _, _, campaign = setup
    contact = app.prepare("automation", seed)
    old = app.store.one("select * from messages")
    app.approve(old["id"])
    campaign.daily_limit = 6
    app.configure(campaign)
    new_id = app.redraft(contact["id"])
    assert app.store.one("select status from messages where id=?", (old["id"],))["status"] == "CANCELLED"
    current = app.store.one("select * from messages where id=?", (new_id,))
    assert current["status"] == "REVIEW"
    assert not current["approved_hash"]
    assert current["config_hash"] != old["config_hash"]


def test_campaign_change_during_composition_does_not_get_current_approval_hash(setup, seed):
    app, model, _, campaign = setup
    original = model.complete

    def change_offer(task, payload):
        result = original(task, payload)
        if task == "edit":
            campaign.offer = "A completely different bounded technical offer, two hours."
            app.configure(campaign)
        return result

    model.complete = change_offer
    with pytest.raises(ValueError, match="configuration"):
        app.prepare("automation", seed)
    assert app.store.one("select count(*) n from messages")["n"] == 0


def test_automatic_discovery_stops_at_target_and_queues_review(setup, seed, monkeypatch):
    import json
    import pipeline.research as research
    from pipeline.settings import SettingsStore

    app, _, mail, campaign = setup
    campaign.discovery_queries = ["documents agencies contact"]
    app.configure(campaign)
    SettingsStore(app.store.path.parent).update({"discovery_limit": 1})
    monkeypatch.setattr(
        research, "search", lambda *a, **k: ["https://example.org/services", "https://other.example/services"]
    )
    calls = []

    def research_company(self, url):
        calls.append(url)
        return seed

    monkeypatch.setattr(research.LeadResearcher, "research_company", research_company)
    result = research.discover(app, campaign.id)
    assert len(result) == 1 and calls == ["https://example.org/services"]
    assert not mail.sent
    assert app.store.one("select status from messages")["status"] == "REVIEW"
    report = json.loads(
        app.store.one("select value from sync_state where key=?", ("discovery_report:" + campaign.id,))[
            "value"
        ]
    )
    assert report["qualified"] == 1 and report["contacts"] == 1 and report["status"] == "DONE"


def test_discovery_reports_source_skips_without_inventing_contacts(setup, monkeypatch):
    import json
    import pipeline.research as research

    app, _, mail, campaign = setup
    campaign.discovery_queries = ["a search"]
    app.configure(campaign)
    monkeypatch.setattr(research, "search", lambda *a, **k: ["https://example.org/services"])

    def unavailable(self, url):
        raise ValueError("No published email found on the checked company pages")

    monkeypatch.setattr(research.LeadResearcher, "research_company", unavailable)
    assert research.discover(app, campaign.id) == []
    report = json.loads(
        app.store.one("select value from sync_state where key=?", ("discovery_report:" + campaign.id,))[
            "value"
        ]
    )
    assert report["skipped"] == 1 and report["status"] == "DONE"
    assert not app.store.all("select * from contacts") and not mail.sent


def test_discovery_existing_email_is_duplicate_not_new_qualified_prospect(setup, seed, monkeypatch):
    import json
    import pipeline.research as research

    app, _, _, campaign = setup
    app.prepare(campaign.id, seed)
    campaign.discovery_queries = ["a search"]
    app.configure(campaign)
    monkeypatch.setattr(research, "search", lambda *a, **k: ["https://alias.example/services"])
    alias = seed.model_copy(update={"domain": "alias.example"})
    monkeypatch.setattr(research.LeadResearcher, "research_company", lambda self, url: alias)
    assert research.discover(app, campaign.id) == []
    report = json.loads(
        app.store.one("select value from sync_state where key=?", ("discovery_report:" + campaign.id,))[
            "value"
        ]
    )
    assert report["duplicates"] == 1 and report["qualified"] == 0 and report["contacts"] == 0


def test_sender_profile_exclusions_block_generated_draft(setup, seed):
    app, model, _, campaign = setup
    app.writer.excluded_terms = ["restricted origin"]
    original = model.complete

    def injected(task, payload):
        result = original(task, payload)
        if task == "write":
            result["subject"] = "My Restricted Origin experience"
        return result

    model.complete = injected
    with pytest.raises(WritingError, match="profile preferences"):
        app.prepare(campaign.id, seed)
    assert not app.store.all("select * from messages")


def test_relationship_mode_retains_imperfect_fit_without_spending_on_draft(setup, seed):
    app, model, mail, campaign = setup
    campaign.prospecting_goal = "RELATIONSHIPS"
    app.configure(campaign)
    original = model.complete
    model.complete = lambda task, payload: (
        {"score": 0.55, "confidence": 0.7, "reason": "Plausible teaching opportunity; budget unknown"}
        if task == "qualify"
        else original(task, payload)
    )
    c = app.prepare(campaign.id, seed)
    assert c["state"] == "QUALIFIED"
    assert not app.store.all("select * from messages") and not mail.sent
    app.prepare_outreach(c["id"])
    assert app.store.one("select status from messages")["status"] == "REVIEW"
    assert not mail.sent


def test_relationship_uncertain_lead_needs_research_instead_of_rejection(setup, seed):
    app, model, _, campaign = setup
    campaign.prospecting_goal = "RELATIONSHIPS"
    app.configure(campaign)
    model.complete = lambda *args: {"score": 0.2, "confidence": 0.4, "reason": "Missing relevant evidence"}
    c = app.prepare(campaign.id, seed)
    assert c["state"] == "NEEDS_RESEARCH"
    assert not app.store.all("select * from messages")


def test_discovery_rotates_queries_and_remembers_unavailable_domains(setup, monkeypatch):
    import pipeline.research as research

    app, _, _, campaign = setup
    campaign.discovery_queries = ["small agency workflow"]
    app.configure(campaign)
    queries, fetches = [], []

    def search(query, **kwargs):
        queries.append(query)
        return ["https://example.org/contact"]

    def unavailable(self, url):
        fetches.append(url)
        raise ValueError("No published email found on the checked company pages")

    monkeypatch.setattr(research, "search", search)
    monkeypatch.setattr(research.LeadResearcher, "research_company", unavailable)
    research.discover(app, campaign.id)
    first = set(queries)
    research.discover(app, campaign.id)
    assert set(queries) - first
    assert len(fetches) == 1
    row = app.store.one("select * from discovery_candidates")
    assert row["status"] == "NEEDS_CONTACT" and row["domain"] == "example.org"
    assert app.store.all("select * from discovery_queries")


def test_explicit_draft_does_not_override_suppression(setup, seed):
    app, _, _, campaign = setup
    campaign.prospecting_goal = "RELATIONSHIPS"
    app.configure(campaign)
    c = app.prepare(campaign.id, seed)
    app.store.execute(
        "insert into suppressions values (?,?,?,?)",
        (seed.email, c["company_id"], "stop", "2026-10-18T00:00:00+00:00"),
    )
    with pytest.raises(ValueError, match="suppressed"):
        app.prepare_outreach(c["id"])


def test_operator_feedback_is_available_to_next_qualification(setup, seed):
    app, model, _, _ = setup
    c = app.prepare("automation", seed)
    app.store.execute(
        "insert into prospect_feedback values (?,?,?,?)",
        (c["id"], "WRONG_BUYER", "Prefer operations staff", "2026-10-18T00:00:00+00:00"),
    )
    other = seed.model_copy(update={"email": "other@other.example", "domain": "other.example"})
    app.prepare("automation", other)
    payload = [p for task, p in model.calls if task == "qualify"][-1]
    assert payload["operator_feedback"][0]["verdict"] == "WRONG_BUYER"


def test_discovery_respects_persistent_lease(setup):
    from pipeline.models import now
    from datetime import timedelta
    from pipeline.outbound import SafeRetry
    from pipeline.research import discover

    app, _, _, _ = setup
    app.store.execute(
        "insert into executions(id,step,status,next_at,updated_at) values (?,'discovery','RUNNING',?,?)",
        ("discovery_lease:automation", (now() + timedelta(minutes=10)).isoformat(), now().isoformat()),
    )
    with pytest.raises(SafeRetry, match="already running"):
        discover(app, "automation")


def test_good_feedback_seeds_similar_company_searches(setup, seed):
    from pipeline.discovery import next_query

    app, _, _, campaign = setup
    campaign.discovery_queries = ["workflow business"]
    app.configure(campaign)
    c = app.prepare(campaign.id, seed)
    app.store.execute(
        "insert into prospect_feedback values (?,?,?,?)",
        (c["id"], "GOOD", "Helpful opportunity", "2026-10-18T00:00:00+00:00"),
    )
    assert "workflow business" in next_query(app, campaign)
    assert "companies similar to Example Agency" in next_query(app, campaign)


def test_goal_reached_never_calls_search(setup, seed, monkeypatch):
    import pipeline.research as research

    app, _, _, campaign = setup
    app.prepare(campaign.id, seed)
    campaign.pool_target = 1
    app.configure(campaign)
    monkeypatch.setattr(
        research, "search", lambda *a, **k: (_ for _ in ()).throw(AssertionError("goal already reached"))
    )
    assert research.discover(app, campaign.id) == []


def test_selected_relationship_draft_survives_budget_retry(setup, seed):
    from pipeline.outbound import SafeRetry

    app, model, _, campaign = setup
    campaign.prospecting_goal = "RELATIONSHIPS"
    app.configure(campaign)
    c = app.prepare(campaign.id, seed)
    original = model.complete

    def fail_write(task, payload):
        if task == "write":
            raise SafeRetry("llm request budget exhausted")
        return original(task, payload)

    model.complete = fail_write
    with pytest.raises(SafeRetry):
        app.prepare_outreach(c["id"])
    model.complete = original
    app.resume(c["id"])
    assert app.store.one("select status from messages")["status"] == "REVIEW"


def test_discovery_budget_pause_is_retryable_not_source_cooldown(setup, monkeypatch):
    from pipeline.outbound import SafeRetry
    from pipeline.models import now
    import pipeline.research as research
    from datetime import datetime, timedelta

    app, _, _, campaign = setup
    campaign.discovery_queries = ["workflow business"]
    app.configure(campaign)
    monkeypatch.setattr(research, "search", lambda *a, **k: ["https://retry.example"])

    def limited(*args):
        raise SafeRetry("llm request budget exhausted")

    monkeypatch.setattr(research.LeadResearcher, "research_company", limited)
    research.discover(app, campaign.id)
    candidate = app.store.one("select * from discovery_candidates")
    assert candidate["status"] == "DEFERRED" and "budget" in candidate["reason"]
    assert datetime.fromisoformat(candidate["next_at"]) < now() + timedelta(minutes=10)
    assert app.store.one("select * from jobs where kind='discover' and status='PENDING'")


def test_expired_candidates_are_not_excluded_from_queries(setup):
    from pipeline.discovery import next_query
    from pipeline.models import now

    app, _, _, campaign = setup
    campaign.discovery_queries = ["workflow business"]
    app.configure(campaign)
    app.store.execute(
        "insert into discovery_candidates(campaign_id,domain,url,status,next_at,updated_at) values (?,?,?,'NEEDS_CONTACT',?,?)",
        (
            campaign.id,
            "expired.example",
            "https://expired.example",
            "2020-01-01T00:00:00+00:00",
            now().isoformat(),
        ),
    )
    assert "-site:expired.example" not in next_query(app, campaign)


def test_relationship_auto_drafts_above_configured_threshold(setup, seed):
    app, _, mail, campaign = setup
    campaign.prospecting_goal = "RELATIONSHIPS"
    campaign.auto_draft = True
    campaign.auto_draft_threshold = 0.5
    app.configure(campaign)
    c = app.prepare(campaign.id, seed)
    assert c["state"] == "READY_FOR_OUTREACH"
    assert app.store.one("select status from messages")["status"] == "REVIEW"
    assert not mail.sent


def test_relationship_auto_draft_threshold_retains_lower_fit(setup, seed):
    app, model, _, campaign = setup
    campaign.prospecting_goal = "RELATIONSHIPS"
    campaign.auto_draft = True
    campaign.auto_draft_threshold = 0.6
    app.configure(campaign)
    model.complete = lambda *args: dict(score=0.55, confidence=0.8, reason="Plausible fit")
    assert app.prepare(campaign.id, seed)["state"] == "QUALIFIED"
    assert not app.store.all("select * from messages")


def test_existing_prospects_are_queued_once_and_respect_unsuitable_feedback(setup, seed):
    from pipeline.worker import Runner

    app, _, mail, campaign = setup
    campaign.prospecting_goal = "RELATIONSHIPS"
    app.configure(campaign)
    app.prepare(campaign.id, seed)
    # Complete the original research job before enabling auto-draft, as in live backfill.
    app.store.execute("update jobs set status='DONE' where kind='prepare'")
    campaign.auto_draft = True
    campaign.auto_draft_threshold = 0.5
    app.configure(campaign)
    assert app.queue_automatic_drafts() == 1
    assert app.queue_automatic_drafts() == 0
    job = app.store.one("select * from jobs where id like 'auto-draft:%'")
    Runner(app).run(job["id"])
    assert app.store.one("select status from messages")["status"] == "REVIEW"
    assert not mail.sent


def test_auto_drafts_skip_suppressed_and_wrong_recipient(setup, seed):
    app, _, _, campaign = setup
    campaign.prospecting_goal = "RELATIONSHIPS"
    app.configure(campaign)
    c = app.prepare(campaign.id, seed)
    app.store.execute(
        "insert into prospect_feedback values (?,?,?,?)",
        (c["id"], "WRONG_BUYER", "Wrong recipient", "2026-10-18T00:00:00+00:00"),
    )
    campaign.auto_draft = True
    app.configure(campaign)
    assert app.queue_automatic_drafts() == 0
    assert not app.store.all("select * from messages")


def test_legacy_campaign_defaults_migrate_hash_before_new_drafts(setup, seed):
    import json
    from pipeline.models import digest
    from pipeline.service import Pipeline

    app, model, mail, campaign = setup
    legacy = campaign.model_dump(mode="json")
    legacy.pop("auto_draft")
    legacy.pop("auto_draft_threshold")
    app.store.execute(
        "update campaigns set config=?,config_hash=?,approved_hash=? where id=?",
        (json.dumps(legacy), digest(legacy), digest(legacy), campaign.id),
    )
    migrated = Pipeline(app.store, model, mail, app.writer.skill_dir)
    row = app.store.one("select * from campaigns where id=?", (campaign.id,))
    assert not row["approved_hash"]
    assert row["config_hash"] == digest(migrated.campaign(campaign.id).model_dump(mode="json"))
    contact = migrated.prepare(campaign.id, seed)
    assert (
        app.store.one("select * from messages where contact_id=?", (contact["id"],))["config_hash"]
        == row["config_hash"]
    )
