from concurrent.futures import ThreadPoolExecutor
import pytest
from pipeline.models import now
from pipeline.outbound import Blocked, SafeRetry, AmbiguousSend


def prepared(setup, seed):
    app, model, mail, campaign = setup
    contact = app.prepare(campaign.id, seed)
    message = app.store.one("select * from messages where contact_id=?", (contact["id"],))
    return app, mail, contact, message


def test_approved_outreach_sends_ids_and_followups(setup, seed):
    app, mail, contact, message = prepared(setup, seed)
    with pytest.raises(Blocked, match="approval"):
        app.send(message["id"])
    app.approve(message["id"])
    assert app.send(message["id"]) == "SENT"
    assert app.send(message["id"]) == "SENT"
    assert len(mail.sent) == 1
    saved = app.store.one("select * from messages where id=?", (message["id"],))
    assert saved["gmail_id"] and saved["thread_id"]
    assert app.store.one("select count(*) n from followups")["n"] == 1


def test_global_pause_stops_approved_send(setup, seed):
    app, mail, _, message = prepared(setup, seed)
    app.approve(message["id"])
    app.store.execute("update settings set value='true' where key='paused'")
    with pytest.raises(Blocked, match="paused"):
        app.send(message["id"])
    assert not mail.sent


def test_config_change_invalidates_approval(setup, seed):
    app, mail, _, message = prepared(setup, seed)
    app.approve(message["id"])
    changed = app.campaign("automation").model_copy(update={"daily_limit": 6})
    app.configure(changed)
    with pytest.raises(Blocked, match="configuration"):
        app.send(message["id"])
    assert not mail.sent


def test_content_tampering_invalidates_approval(setup, seed):
    app, mail, _, message = prepared(setup, seed)
    app.approve(message["id"])
    app.store.execute("update messages set body=? where id=?", ("Tampered", message["id"]))
    with pytest.raises(Blocked, match="content"):
        app.send(message["id"])
    assert not mail.sent


def test_unknown_region_and_unverified_address_blocked(setup, seed):
    seed.region_reviewed = False
    app, mail, _, message = prepared(setup, seed)
    with pytest.raises(Blocked, match="US"):
        app.approve(message["id"])
    # The final send check also protects previously approved contacts whose review changed.
    app.store.execute("update messages set approved_hash=content_hash where id=?", (message["id"],))
    with pytest.raises(Blocked, match="region"):
        app.send(message["id"])
    assert not mail.sent


def test_autonomous_requires_exact_config_authorization(setup, seed):
    app, _, mail, campaign = setup
    from pipeline.models import Mode

    campaign.mode = Mode.AUTONOMOUS
    app.configure(campaign)
    contact = app.prepare(campaign.id, seed)
    message = app.store.one("select * from messages where contact_id=?", (contact["id"],))
    with pytest.raises(Blocked, match="authorization"):
        app.send(message["id"])
    app.store.execute("update campaigns set approved_hash=config_hash where id=?", (campaign.id,))
    assert app.send(message["id"]) == "SENT"
    assert len(mail.sent) == 1


def test_concurrent_workers_send_once(setup, seed):
    app, mail, _, message = prepared(setup, seed)
    app.approve(message["id"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: app.send(message["id"]), range(4)))
    assert set(results) <= {"SENT", "SENDING"}
    assert "SENT" in results
    assert len(mail.sent) == 1


def test_safe_provider_rejection_can_retry(setup, seed):
    app, mail, _, message = prepared(setup, seed)
    app.approve(message["id"])
    mail.error = SafeRetry("rate limited before acceptance")
    with pytest.raises(SafeRetry):
        app.send(message["id"])
    assert app.store.one("select status from messages")["status"] == "APPROVED"
    mail.error = None
    with pytest.raises(Blocked, match="cooldown"):
        app.send(message["id"])
    app.store.execute("delete from sync_state where key='gmail_send_not_before'")
    assert app.send(message["id"]) == "SENT"


def test_ambiguous_timeout_never_retries_blindly(setup, seed):
    app, mail, _, message = prepared(setup, seed)
    app.approve(message["id"])
    mail.error = AmbiguousSend("response lost")
    assert app.send(message["id"]) == "UNKNOWN"
    mail.error = None
    assert app.send(message["id"]) == "UNKNOWN"
    assert not mail.sent
    assert app.store.one("select count(*) n from escalations")["n"] == 1


def test_global_suppression_blocks_cross_campaign(setup, seed):
    app, mail, contact, message = prepared(setup, seed)
    app.store.execute(
        "insert into suppressions values (?,?,?,?)",
        (seed.email, contact["company_id"], "unsubscribe", now().isoformat()),
    )
    app.approve(message["id"])
    with pytest.raises(Blocked, match="suppressed"):
        app.send(message["id"])
    assert not mail.sent


def test_daily_limit_is_global_across_campaigns(setup, seed):
    app, mail, contact, message = prepared(setup, seed)
    app.approve(message["id"])
    # A reserved or ambiguous send counts towards limits too.
    app.store.execute("update messages set status='UNKNOWN',sent_at=?", (now().isoformat(),))
    app.configure(app.campaign("automation").model_copy(update={"global_daily_limit": 1}))
    other = seed.model_copy(update={"email": "other@other.org", "domain": "other.org"})
    c = app.prepare("automation", other)
    m = app.store.one("select * from messages where contact_id=?", (c["id"],))
    app.approve(m["id"])
    with pytest.raises(Blocked, match="limit"):
        app.send(m["id"])
    assert not mail.sent


def test_postal_address_optional_for_reviewed_outreach(setup, seed):
    app, _, mail, campaign = setup
    campaign.postal_address = ""
    app.configure(campaign)
    contact = app.prepare(campaign.id, seed)
    message = app.store.one("select * from messages where contact_id=?", (contact["id"],))
    assert "\n\nAaron\nBusiness solicitation." in message["body"]
    with pytest.raises(Blocked, match="approval"):
        app.send(message["id"])
    app.approve(message["id"])
    assert app.send(message["id"]) == "SENT"
    assert len(mail.sent) == 1


def test_new_profile_exclusion_blocks_existing_approved_email(setup, seed):
    app, mail, _, message = prepared(setup, seed)
    app.approve(message["id"])
    app.writer.excluded_terms = ["document workflow"]
    with pytest.raises(Blocked, match="profile preferences"):
        app.send(message["id"])
    assert not mail.sent


def test_us_confirmation_approves_and_sends_without_notes(setup, seed):
    from pipeline.models import Seed

    seed.region, seed.region_reviewed = "UNKNOWN", False
    seed.legal_basis = seed.provenance = ""
    app, mail, contact, message = prepared(setup, seed)
    app.approve(message["id"], confirm_us=True)
    saved = Seed.model_validate_json(
        app.store.one("select seed from contacts where id=?", (contact["id"],))["seed"]
    )
    assert saved.region == "US" and saved.region_reviewed
    assert saved.legal_basis == saved.provenance == ""
    assert app.send(message["id"]) == "SENT"
    assert len(mail.sent) == 1


def test_unreviewed_contact_cannot_be_approved_without_confirmation(setup, seed):
    seed.region_reviewed = False
    app, mail, contact, message = prepared(setup, seed)
    with pytest.raises(Blocked, match="US"):
        app.approve(message["id"])
    assert app.store.one("select status from messages where id=?", (message["id"],))["status"] == "REVIEW"


def test_footer_replacement_preserves_copy_and_requires_review(setup, seed):
    from pipeline.db import Store
    from pipeline.outbound import content_hash

    app, mail, contact, message = prepared(setup, seed)
    old = 'Business solicitation. Reply "unsubscribe" to stop all outreach.'
    body = message["body"].replace(
        "Let me know if you want me to stop contacting you.", 'Reply "unsubscribe" to stop all outreach.'
    )
    message["body"] = body
    app.store.execute(
        "update messages set body=?,content_hash=? where id=?", (body, content_hash(message), message["id"])
    )
    app.approve(message["id"])
    calls = len(app.model.calls)
    store = Store(app.store.path)
    saved = store.one("select * from messages where id=?", (message["id"],))
    assert saved["body"] == body.replace(
        old, "Business solicitation. Let me know if you want me to stop contacting you."
    )
    assert saved["status"] == "REVIEW" and saved["approved_hash"] is None
    assert saved["content_hash"] == content_hash(saved)
    assert len(app.model.calls) == calls
    app.approve(message["id"])
    assert app.send(message["id"]) == "SENT"


def test_blank_optional_notes_preserve_existing_research(setup, seed):
    app, _, contact, message = prepared(setup, seed)
    app.approve(message["id"], confirm_us=True, legal_basis="", provenance="  ")
    from pipeline.models import Seed

    saved = Seed.model_validate_json(
        app.store.one("select seed from contacts where id=?", (contact["id"],))["seed"]
    )
    assert saved.legal_basis == seed.legal_basis
    assert saved.provenance == seed.provenance


def test_replaced_approved_footer_requires_review_even_in_autonomous_mode(setup, seed):
    from pipeline.db import Store
    from pipeline.models import Mode
    from pipeline.outbound import content_hash

    app, _, _, campaign = setup
    campaign.mode = Mode.AUTONOMOUS
    app.configure(campaign)
    app.store.execute("update campaigns set approved_hash=config_hash")
    app, mail, _, message = prepared(setup, seed)
    message["body"] = message["body"].replace(
        "Let me know if you want me to stop contacting you.", 'Reply "unsubscribe" to stop all outreach.'
    )
    app.store.execute(
        "update messages set body=?,content_hash=? where id=?",
        (message["body"], content_hash(message), message["id"]),
    )
    app.approve(message["id"])
    Store(app.store.path)
    with pytest.raises(Blocked, match="approval"):
        app.send(message["id"])
    app.approve(message["id"])
    assert app.send(message["id"]) == "SENT"


def test_cooldown_set_after_reservation_still_prevents_provider_call(setup, seed, monkeypatch):
    from contextlib import contextmanager
    from datetime import timedelta

    app, mail, _, message = prepared(setup, seed)
    app.approve(message["id"])
    original = app.store.tx
    transactions = []

    @contextmanager
    def tx():
        with original() as db:
            yield db
        transactions.append(True)
        if len(transactions) == 1:
            with original() as db:
                db.execute(
                    "insert into sync_state values ('gmail_send_not_before',?)",
                    ((now() + timedelta(hours=2)).isoformat(),),
                )

    monkeypatch.setattr(app.store, "tx", tx)
    with pytest.raises(SafeRetry):
        app.send(message["id"])
    assert not mail.sent
    assert app.store.one("select status from messages where id=?", (message["id"],))["status"] == "APPROVED"


def test_reapproval_clears_obsolete_queue_error_and_old_retry_date(setup, seed):
    from datetime import timedelta, datetime

    app, _, _, message = prepared(setup, seed)
    app.store.enqueue("send:" + message["id"], "send", {"message_id": message["id"]})
    app.store.execute(
        "update jobs set error='Message requires approval',due_at=? where kind='send'",
        ((now() - timedelta(days=3)).isoformat(),),
    )
    app.approve(message["id"])
    job = app.store.one("select * from jobs where kind='send'")
    assert job["error"] is None
    assert datetime.fromisoformat(job["due_at"]) > now() - timedelta(seconds=2)
