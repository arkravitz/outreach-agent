import pytest
from test_outbound import prepared


def sent(setup, seed):
    app, mail, contact, message = prepared(setup, seed)
    app.approve(message["id"])
    app.send(message["id"])
    return app, mail, contact, message


def event(contact, text, id="incoming-1"):
    return {
        "id": id,
        "thread_id": "thread-" + str(contact["id"]),
        "sender": "sam@example.org",
        "body": text,
        "in_reply_to": "",
    }


def test_reply_cancels_followups_before_classification(setup, seed):
    app, _, contact, _ = sent(setup, seed)
    assert app.ingest(event(contact, "Sounds interesting, let us talk."))
    assert app.store.one("select status from followups")["status"] == "CANCELLED"
    assert app.store.one("select reply_hold from contacts")["reply_hold"] == 1
    assert app.classify("incoming-1")["category"] == "INTERESTED"
    assert app.store.one("select state from contacts")["state"] == "ENGAGED"
    assert app.store.one("select count(*) n from opportunities")["n"] == 1


@pytest.mark.parametrize(
    "text,category,state",
    [
        ("Not interested, thanks.", "NOT_INTERESTED", "LOST"),
        ("Please unsubscribe me.", "UNSUBSCRIBE", "SUPPRESSED"),
        ("Remove me from your list", "UNSUBSCRIBE", "SUPPRESSED"),
        ("Address not found. 550 5.1.1", "BOUNCE", "BOUNCED"),
        ("Out of office until next week.", "OUT_OF_OFFICE", "REVIEW"),
        ("Maybe this is a fit under some conditions.", "UNCERTAIN", "REVIEW"),
    ],
)
def test_classification_actions(setup, seed, text, category, state):
    app, _, contact, _ = sent(setup, seed)
    e = event(contact, text)
    if category == "BOUNCE":
        e["sender"] = "mailer-daemon@googlemail.com"
        e["bounce"] = True
    app.ingest(e)
    result = app.classify(e["id"])
    assert result["category"] == category
    assert app.store.one("select state from contacts")["state"] == state
    if category == "UNSUBSCRIBE":
        assert app.store.one("select email from suppressions")["email"] == seed.email
    if category == "BOUNCE":
        assert app.store.one("select invalid from contacts")["invalid"] == 1


def test_duplicate_inbound_processed_once(setup, seed):
    app, _, contact, _ = sent(setup, seed)
    e = event(contact, "I am interested")
    assert app.ingest(e)
    assert not app.ingest(e)
    app.classify(e["id"])
    app.classify(e["id"])
    assert app.store.one("select count(*) n from inbound")["n"] == 1
    assert app.store.one("select count(*) n from opportunities")["n"] == 1


def test_unrelated_thread_ignored(setup, seed):
    app, _, _, _ = sent(setup, seed)
    assert not app.ingest(
        {"id": "private", "thread_id": "other", "sender": "friend@example.com", "body": "Private"}
    )
    assert app.store.one("select count(*) n from inbound")["n"] == 0


def test_sender_spoof_does_not_match_known_thread(setup, seed):
    app, _, contact, _ = sent(setup, seed)
    e = event(contact, "unsubscribe")
    e["sender"] = "stranger@example.net"
    assert not app.ingest(e)
    assert app.store.one("select count(*) n from suppressions")["n"] == 0


def test_quote_of_original_optout_does_not_unsubscribe(setup, seed):
    app, _, contact, _ = sent(setup, seed)
    app.ingest(
        event(
            contact, 'I am interested\n\nOn Friday Aaron wrote:\n> Reply "unsubscribe" to stop all outreach.'
        )
    )
    assert app.classify("incoming-1")["category"] == "INTERESTED"
