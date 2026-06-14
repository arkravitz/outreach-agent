import base64
import httpx
import pytest
from pipeline.gmail import Gmail, GmailSync
from pipeline.outbound import SafeRetry, AmbiguousSend
from test_outbound import prepared


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_gmail_plain_text_encoding_and_thread_headers():
    requests = []

    def handler(request):
        import json
        from email import message_from_bytes

        requests.append(request)
        body = json.loads(request.content)
        raw = message_from_bytes(base64.urlsafe_b64decode(body["raw"]))
        assert raw["Message-ID"] == "<unique@example.com>"
        assert raw["In-Reply-To"] == "<previous@example.org>"
        assert body["threadId"] == "known-thread"
        assert raw.get_content_type() == "text/plain"
        return httpx.Response(200, json={"id": "gmail-1", "threadId": "known-thread"})

    gmail = Gmail(client(handler))
    result = gmail.send(
        {
            "from": "aaron@example.com",
            "from_name": "Aaron",
            "to": "sam@example.org",
            "subject": "Document question",
            "body": "A useful question",
            "rfc_id": "<unique@example.com>",
            "reply_to": "<previous@example.org>",
            "thread_id": "known-thread",
        }
    )
    assert result["id"] == "gmail-1"
    assert len(requests) == 1


@pytest.mark.parametrize("status,exc", [(429, SafeRetry), (500, AmbiguousSend), (503, AmbiguousSend)])
def test_gmail_does_not_retry_send_requests(status, exc):
    count = []

    def handler(request):
        count.append(1)
        return httpx.Response(status, json={"error": "failure"})

    gmail = Gmail(client(handler))
    with pytest.raises(exc):
        gmail.send(
            {
                "from": "aaron@example.com",
                "from_name": "Aaron",
                "to": "sam@example.org",
                "subject": "Question",
                "body": "Hello",
                "rfc_id": "<u@example.com>",
            }
        )
    assert len(count) == 1


def test_history_checkpoint_replay_dedup_and_no_unrelated_content(setup, seed):
    app, mail, contact, message = prepared(setup, seed)
    app.approve(message["id"])
    app.send(message["id"])
    app.store.execute("insert into sync_state values ('gmail_history','10')")
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/history"):
            return httpx.Response(
                200,
                json={
                    "historyId": "20",
                    "history": [
                        {
                            "messagesAdded": [
                                {"message": {"id": "reply-1", "threadId": "thread-" + str(contact["id"])}},
                                {"message": {"id": "private", "threadId": "private-thread"}},
                            ]
                        }
                    ],
                },
            )
        if request.url.path.endswith("/messages/private"):
            assert request.url.params.get("format") == "metadata"
            return httpx.Response(200, json={"id": "private", "payload": {"headers": []}})
        if request.url.path.endswith("/messages/reply-1"):
            return httpx.Response(
                200,
                json={
                    "id": "reply-1",
                    "threadId": "thread-" + str(contact["id"]),
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [{"name": "From", "value": seed.email}],
                        "body": {"data": base64.urlsafe_b64encode(b"I am interested").decode()},
                    },
                },
            )
        raise AssertionError(request.url)

    sync = GmailSync(app, Gmail(client(handler)))
    sync.poll()
    sync.poll()
    assert app.store.one("select count(*) n from inbound")["n"] == 1
    assert app.store.one("select status from followups")["status"] == "CANCELLED"
    assert app.store.one("select value from sync_state where key='gmail_history'")["value"] == "20"
    assert not any("private-thread" in p for p in seen)


def test_expired_history_recovers_only_tracked_threads(setup, seed):
    app, mail, contact, message = prepared(setup, seed)
    app.approve(message["id"])
    app.send(message["id"])
    app.store.execute("insert into sync_state values ('gmail_history','expired')")

    def handler(request):
        if request.url.path.endswith("/history"):
            return httpx.Response(404, json={})
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"historyId": "fresh"})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": []})
        if "/threads/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {
                            "id": "new-reply",
                            "threadId": "thread-" + str(contact["id"]),
                            "payload": {
                                "mimeType": "text/plain",
                                "headers": [{"name": "From", "value": seed.email}],
                                "body": {"data": base64.urlsafe_b64encode(b"Not interested").decode()},
                            },
                        }
                    ]
                },
            )
        raise AssertionError(request.url)

    GmailSync(app, Gmail(client(handler))).poll()
    assert app.store.one("select count(*) n from inbound")["n"] == 1
    assert app.store.one("select value from sync_state where key='gmail_history'")["value"] == "fresh"


def test_checkpoint_not_advanced_on_fetch_failure(setup, seed):
    app, mail, contact, message = prepared(setup, seed)
    app.approve(message["id"])
    app.send(message["id"])
    app.store.execute("insert into sync_state values ('gmail_history','10')")

    def handler(request):
        if request.url.path.endswith("/history"):
            return httpx.Response(
                200,
                json={
                    "historyId": "20",
                    "history": [
                        {
                            "messagesAdded": [
                                {"message": {"id": "r", "threadId": "thread-" + str(contact["id"])}}
                            ]
                        }
                    ],
                },
            )
        return httpx.Response(503, json={})

    with pytest.raises(httpx.HTTPStatusError):
        GmailSync(app, Gmail(client(handler))).poll()
    assert app.store.one("select value from sync_state where key='gmail_history'")["value"] == "10"


def test_reply_rfc_message_id_is_persisted_for_correct_threading(setup, seed):
    from pipeline.gmail import parse_message

    app, mail, contact, message = prepared(setup, seed)
    app.approve(message["id"])
    app.send(message["id"])
    incoming = {
        "id": "gmail-internal-id",
        "threadId": "thread-" + str(contact["id"]),
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": seed.email},
                {"name": "Message-ID", "value": "<actual-reply@example.org>"},
            ],
            "body": {"data": base64.urlsafe_b64encode(b"I am interested").decode()},
        },
    }
    app.ingest(parse_message(incoming))
    assert app.store.one("select rfc_id from inbound")["rfc_id"] == "<actual-reply@example.org>"


def test_followup_preserves_original_subject_before_grounding(setup, seed):
    from pipeline.worker import Runner
    from pipeline.models import now

    app, mail, contact, message = prepared(setup, seed)
    app.approve(message["id"])
    app.send(message["id"])
    app.store.execute("update messages set subject=? where id=?", ("Original subject", message["id"]))
    app.store.execute("update followups set due_at=?", (now().isoformat(),))
    follow = app.store.one("select id from followups")
    Runner(app).followup(follow["id"])
    assert (
        app.store.one("select subject from messages where category='followup'")["subject"]
        == "Original subject"
    )


def test_new_thread_reply_matched_by_rfc_header_before_checkpoint(setup, seed):
    app, mail, contact, message = prepared(setup, seed)
    app.approve(message["id"])
    app.send(message["id"])
    app.store.execute("insert into sync_state values ('gmail_history','10')")
    formats = []

    def handler(request):
        if request.url.path.endswith("/history"):
            return httpx.Response(
                200,
                json={
                    "historyId": "20",
                    "history": [
                        {
                            "messagesAdded": [
                                {"message": {"id": "new-thread-reply", "threadId": "different-thread"}}
                            ]
                        }
                    ],
                },
            )
        formats.append(request.url.params.get("format"))
        return httpx.Response(
            200,
            json={
                "id": "new-thread-reply",
                "threadId": "different-thread",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "From", "value": seed.email},
                        {"name": "In-Reply-To", "value": message["rfc_id"]},
                    ],
                    "body": {"data": base64.urlsafe_b64encode(b"Please unsubscribe").decode()},
                },
            },
        )

    GmailSync(app, Gmail(client(handler))).poll()
    assert app.store.one("select email from suppressions")["email"] == seed.email
    assert app.store.one("select status from followups")["status"] == "CANCELLED"
    assert formats == ["metadata", "full"]


def test_machine_readable_dsn_in_new_thread_disables_address(setup, seed):
    from pipeline.gmail import parse_message

    app, mail, contact, message = prepared(setup, seed)
    app.approve(message["id"])
    app.send(message["id"])
    dsn = (
        "Final-Recipient: rfc822; sam@example.org\r\nAction: failed\r\nStatus: 5.1.1\r\nOriginal-Message-ID: "
        + message["rfc_id"]
    )
    incoming = {
        "id": "dsn-1",
        "threadId": "dsn-separate-thread",
        "payload": {
            "mimeType": "multipart/report",
            "headers": [{"name": "From", "value": "mailer-daemon@googlemail.com"}],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {
                        "data": base64.urlsafe_b64encode(
                            b"Delivery to the following recipient failed."
                        ).decode()
                    },
                },
                {
                    "mimeType": "message/delivery-status",
                    "body": {"data": base64.urlsafe_b64encode(dsn.encode()).decode()},
                },
            ],
        },
    }
    e = parse_message(incoming)
    assert e["bounce"]
    assert app.ingest(e)
    assert app.store.one("select invalid from contacts")["invalid"] == 1
    assert app.store.one("select status from followups")["status"] == "CANCELLED"


def test_poll_before_any_outreach_does_not_read_mailbox(setup, seed):
    app, _, _, _ = setup
    app.prepare("automation", seed)

    def handler(request):
        raise AssertionError("Unsent drafts must not trigger mailbox access")

    GmailSync(app, Gmail(client(handler))).poll()
    assert not app.store.all("select * from inbound")


def test_unrelated_delivery_report_body_is_not_fetched(setup, seed):
    app, _, _, message = prepared(setup, seed)
    app.approve(message["id"])
    app.send(message["id"])

    def handler(request):
        assert request.url.params.get("format") == "metadata"
        return httpx.Response(
            200, json={"payload": {"headers": [{"name": "Content-Type", "value": "multipart/report"}]}}
        )

    GmailSync(app, Gmail(client(handler))).inspect_candidate(
        {"id": "unrelated", "threadId": "unrelated-thread"}, set()
    )
    assert not app.store.all("select * from inbound")


@pytest.mark.parametrize("status,reason", [(429, "rateLimitExceeded"), (403, "userRateLimitExceeded")])
def test_gmail_rate_limit_preserves_retry_after(status, reason):
    gmail = Gmail(
        client(
            lambda request: httpx.Response(
                status, headers={"Retry-After": "7200"}, json={"error": {"errors": [{"reason": reason}]}}
            )
        )
    )
    with pytest.raises(SafeRetry) as caught:
        gmail.send(
            {
                "from": "a@example.com",
                "from_name": "A",
                "to": "b@example.org",
                "subject": "Question",
                "body": "Hello",
                "rfc_id": "<test@example.com>",
            }
        )
    assert caught.value.retry_after_seconds == 7200
