import http.client
import json
import threading
import pytest
from contextlib import contextmanager
from pipeline.web import create_server
from pipeline.settings import SettingsStore


@contextmanager
def server(tmp_path):
    httpd = create_server(tmp_path, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def connection(httpd):
    return http.client.HTTPConnection("127.0.0.1", httpd.server_port)


def login(httpd):
    conn = connection(httpd)
    conn.request("GET", "/api/session")
    response = conn.getresponse()
    data = json.loads(response.read())
    return {
        "Cookie": response.getheader("Set-Cookie").split(";")[0],
        "X-Pipeline-CSRF": data["csrf"],
        "Content-Type": "application/json",
        "Origin": f"http://127.0.0.1:{httpd.server_port}",
    }


def test_setup_page_and_empty_state_are_accessible(tmp_path):
    with server(tmp_path) as httpd:
        conn = connection(httpd)
        conn.request("GET", "/")
        response = conn.getresponse()
        html = response.read().decode()
        assert response.status == 200 and "Pipeline" in html
        assert "Content-Security-Policy" in dict(response.getheaders())
        headers = login(httpd)
        conn.request("GET", "/api/snapshot", headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read())
        assert response.status == 200
        assert len(data["campaigns"]) == 2
        assert not data["contacts"]
        assert not data["setup"]["ready_to_send"]
        assert data["settings"]["provider"] == "ollama"


def test_web_requires_session_csrf_and_same_origin(tmp_path):
    with server(tmp_path) as httpd:
        conn = connection(httpd)
        conn.request("GET", "/api/snapshot")
        assert conn.getresponse().status == 403
        conn.close()
        for headers in [{}, {"Origin": "https://evil.example"}, {"Host": "evil.example"}]:
            conn = connection(httpd)
            conn.request("POST", "/api/pause", "{}", headers={"Content-Type": "application/json", **headers})
            response = conn.getresponse()
            response.read()
            assert response.status == 403
            conn.close()


def test_ui_saves_settings_without_leaking_secret_and_pauses(tmp_path):
    with server(tmp_path) as httpd:
        headers = login(httpd)
        conn = connection(httpd)
        conn.request(
            "POST",
            "/api/settings",
            json.dumps({"provider": "gemini", "api_key": "very-private", "default_model": "gemini-test"}),
            headers,
        )
        response = conn.getresponse()
        result = response.read().decode()
        assert response.status == 200
        assert "very-private" not in result
        assert SettingsStore(tmp_path).load().api_key == "very-private"
        conn.request("POST", "/api/pause", '{"paused":true}', headers)
        response = conn.getresponse()
        assert response.status == 200
        response.read()
        conn.request("GET", "/api/snapshot", headers=headers)
        response = conn.getresponse()
        assert json.loads(response.read())["paused"] is True


def test_google_oauth_start_requires_uploaded_client_and_state(tmp_path):
    with server(tmp_path) as httpd:
        headers = login(httpd)
        conn = connection(httpd)
        conn.request("POST", "/api/google/start", "{}", headers)
        response = conn.getresponse()
        assert response.status == 400
        response.read()
        conn.request("GET", "/oauth/callback?state=bad&code=bad")
        response = conn.getresponse()
        assert response.status == 400
        response.read()


def test_settings_cannot_inject_server_paths(tmp_path):
    with server(tmp_path) as httpd:
        headers = login(httpd)
        conn = connection(httpd)
        conn.request("POST", "/api/settings", '{"token_path":"/tmp/hijacked"}', headers)
        response = conn.getresponse()
        assert response.status == 400
        response.read()


def test_oauth_client_upload_never_echoes_secret_and_requests_loopback_callback(tmp_path):
    from urllib.parse import urlsplit, parse_qs

    with server(tmp_path) as httpd:
        headers = login(httpd)
        conn = connection(httpd)
        client = {
            "installed": {
                "client_id": "synthetic.apps.googleusercontent.com",
                "client_secret": "synthetic-client-secret",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        conn.request("POST", "/api/google/client", json.dumps({"client": client}), headers)
        response = conn.getresponse()
        assert response.status == 200
        assert "synthetic-client-secret" not in response.read().decode()
        conn.request("POST", "/api/google/start", "{}", headers)
        response = conn.getresponse()
        data = json.loads(response.read())
        assert response.status == 200
        params = parse_qs(urlsplit(data["url"]).query)
        assert params["redirect_uri"] == [f"http://127.0.0.1:{httpd.server_port}/oauth/callback"]
        assert params["state"]
        assert params["code_challenge"] and params["code_challenge_method"] == ["S256"]
        assert params["prompt"] == ["select_account consent"]
        assert "gmail.compose" in params["scope"][0]
        assert "synthetic-client-secret" not in data["url"]


def test_background_action_status_survives_ui_restart(tmp_path):
    import time
    from pipeline.web_api import WebAPI

    api = WebAPI(tmp_path)
    result = api.background_task("Synthetic durable status", lambda: {"ok": True})
    for _ in range(50):
        row = api.store.one("select status from executions where id=?", ("ui:" + result["task_id"],))
        if row and row["status"] == "DONE":
            break
        time.sleep(0.01)
    reloaded = WebAPI(tmp_path)
    task = reloaded.snapshot()["background"][0]
    assert task["name"] == "Synthetic durable status"
    assert task["status"] == "DONE" and task["result"] == {"ok": True}


def test_oauth_client_rejects_competing_web_section(tmp_path):
    with server(tmp_path) as httpd:
        headers = login(httpd)
        conn = connection(httpd)
        installed = {
            "client_id": "synthetic.apps.googleusercontent.com",
            "client_secret": "synthetic",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        mixed = {
            "installed": installed,
            "web": {
                **installed,
                "auth_uri": "https://example.invalid/authorize",
                "token_uri": "https://example.invalid/token",
            },
        }
        conn.request("POST", "/api/google/client", json.dumps({"client": mixed}), headers)
        response = conn.getresponse()
        response.read()
        assert response.status == 400
        assert not (tmp_path / "google-client.json").exists()


def test_prospect_feedback_and_snapshot_separate_fit_confidence_and_pool(setup, seed):
    from pipeline.web_api import WebAPI

    app, _, _, campaign = setup
    c = app.prepare(campaign.id, seed)
    api = WebAPI(app.store.path.parent)
    result = api.post(
        "/api/prospect/feedback",
        {"id": c["id"], "verdict": "GOOD", "note": "Good person for a workshop"},
        "http://127.0.0.1:8765",
        "test",
    )
    assert result["saved"]
    snap = api.snapshot()
    contact = next(r for r in snap["contacts"] if r["id"] == c["id"])
    assert contact["assessment_confidence"] == 0.95
    assert contact["feedback"]["verdict"] == "GOOD"
    assert snap["prospect_pool"]["contacts"] == 1
    assert "discovery_candidates" in snap
    import pytest

    with pytest.raises(ValueError):
        api.post("/api/prospect/feedback", {"id": c["id"], "verdict": "MAKE_UP_FACTS"}, "", "")


def test_screening_confirmation_reviews_contact_and_queues_email_without_notes(setup, seed, monkeypatch):
    from pipeline.web_api import WebAPI
    from pipeline.models import Seed

    app, model, mail, campaign = setup
    seed.region_reviewed = False
    seed.legal_basis = seed.provenance = ""
    contact = app.prepare(campaign.id, seed)
    message = app.store.one("select * from messages where contact_id=?", (contact["id"],))
    api = WebAPI(app.store.path.parent)
    monkeypatch.setattr(api, "app", lambda: app)
    monkeypatch.setattr(api.worker, "start", lambda: {"running": True})
    calls = len(model.calls)
    result = api.post(
        "/api/message/approve",
        {
            "ids": [message["id"]],
            "confirm_live_sending": True,
            "confirm_us": True,
        },
        "",
        "",
    )
    assert result["approved"] == [message["id"]]
    saved = Seed.model_validate_json(
        app.store.one("select seed from contacts where id=?", (contact["id"],))["seed"]
    )
    assert saved.region_reviewed and saved.region == "US"
    assert saved.legal_basis == saved.provenance == ""
    assert app.store.one("select status from messages where id=?", (message["id"],))["status"] == "APPROVED"
    assert app.store.one("select status from jobs where kind='send'")["status"] == "PENDING"
    assert not mail.sent and len(model.calls) == calls


def test_stale_draft_does_not_record_us_confirmation(setup, seed):
    import pytest
    from pipeline.outbound import Blocked
    from pipeline.models import Seed

    app, _, _, campaign = setup
    seed.region_reviewed = False
    contact = app.prepare(campaign.id, seed)
    message = app.store.one("select * from messages where contact_id=?", (contact["id"],))
    app.configure(campaign.model_copy(update={"daily_limit": 6}))
    with pytest.raises(Blocked, match="configuration"):
        app.approve(message["id"], confirm_us=True)
    saved = Seed.model_validate_json(
        app.store.one("select seed from contacts where id=?", (contact["id"],))["seed"]
    )
    assert not saved.region_reviewed


def test_bulk_send_approves_more_than_fifty_and_starts_worker(setup, seed, monkeypatch):
    from pipeline.web_api import WebAPI

    app, model, mail, campaign = setup
    for i in range(51):
        prospect = seed.model_copy(
            update={"domain": f"company{i}.org", "email": f"sam@company{i}.org", "region_reviewed": False}
        )
        app.prepare(campaign.id, prospect)
    api = WebAPI(app.store.path.parent)
    monkeypatch.setattr(api, "app", lambda: app)
    monkeypatch.setattr(api, "readiness", lambda: {"ready_to_send": True})
    starts = []
    monkeypatch.setattr(api.worker, "start", lambda: starts.append(True) or {"running": True})
    first = app.store.one("select id from messages order by created_at limit 1")["id"]
    app.store.enqueue("send:" + first, "send", {"message_id": first})
    app.store.execute("update jobs set status='CANCELLED' where id=?", ("send:" + first,))
    calls = len(model.calls)
    result = api.post("/api/message/approve-all", {"confirm_us": True, "confirm_live_sending": True}, "", "")
    assert len(result["approved"]) == 51
    assert result["skipped"] == []
    assert starts == [True]
    assert app.store.one("select count(*) n from messages where status='APPROVED'")["n"] == 51
    assert app.store.one("select count(*) n from jobs where kind='send' and status='PENDING'")["n"] == 51
    assert not mail.sent and len(model.calls) == calls
    # Repeated clicks do not duplicate jobs.
    api.post("/api/message/approve-all", {"confirm_us": True, "confirm_live_sending": True}, "", "")
    assert app.store.one("select count(*) n from jobs where kind='send'")["n"] == 51


def test_bulk_send_skips_stale_and_suppressed_contacts(setup, seed, monkeypatch):
    from pipeline.web_api import WebAPI

    app, _, _, campaign = setup
    a = app.prepare(campaign.id, seed)
    other = seed.model_copy(update={"domain": "other.org", "email": "sam@other.org"})
    b = app.prepare(campaign.id, other)
    app.store.execute("update messages set config_hash='obsolete' where contact_id=?", (a["id"],))
    app.store.execute("update contacts set state='SUPPRESSED' where id=?", (b["id"],))
    api = WebAPI(app.store.path.parent)
    monkeypatch.setattr(api, "app", lambda: app)
    monkeypatch.setattr(api, "readiness", lambda: {"ready_to_send": True})
    starts = []
    monkeypatch.setattr(api.worker, "start", lambda: starts.append(True))
    result = api.post("/api/message/approve-all", {"confirm_us": True, "confirm_live_sending": True}, "", "")
    assert result["approved"] == []
    assert len(result["skipped"]) == 2
    assert not starts
    assert app.store.one("select count(*) n from messages where status='APPROVED'")["n"] == 0


def test_bulk_send_requires_confirmation_and_ready_setup(setup, seed, monkeypatch):
    import pytest
    from pipeline.web_api import WebAPI

    app, _, _, campaign = setup
    app.prepare(campaign.id, seed)
    api = WebAPI(app.store.path.parent)
    monkeypatch.setattr(api, "app", lambda: app)
    monkeypatch.setattr(api, "readiness", lambda: {"ready_to_send": False})
    for data in [{}, {"confirm_live_sending": True}, {"confirm_live_sending": True, "confirm_us": True}]:
        with pytest.raises(ValueError):
            api.post("/api/message/approve-all", data, "", "")
    assert app.store.one("select count(*) n from messages where status='APPROVED'")["n"] == 0


def test_bulk_send_keeps_queue_if_worker_cannot_start(setup, seed, monkeypatch):
    from pipeline.web_api import WebAPI

    app, _, _, campaign = setup
    app.prepare(campaign.id, seed)
    api = WebAPI(app.store.path.parent)
    monkeypatch.setattr(api, "app", lambda: app)
    monkeypatch.setattr(api, "readiness", lambda: {"ready_to_send": True})

    def fail_start():
        raise OSError("Worker executable unavailable")

    monkeypatch.setattr(api.worker, "start", fail_start)
    result = api.post("/api/message/approve-all", {"confirm_us": True, "confirm_live_sending": True}, "", "")
    assert len(result["approved"]) == 1
    assert result["worker_error"] == "Worker executable unavailable"
    assert app.store.one("select status from jobs where kind='send'")["status"] == "PENDING"
    assert app.store.one("select status from messages")["status"] == "APPROVED"


def test_sent_email_history_pages_past_snapshot_and_searches(setup, seed):
    from pipeline.web_api import WebAPI

    app, _, _, campaign = setup
    app.prepare(campaign.id, seed)
    original = app.store.one("select * from messages limit 1")
    with app.store.tx() as db:
        for i in range(205):
            row = {
                **original,
                "id": f"history-{i:03}",
                "rfc_id": f"<history-{i}@example.com>",
                "status": "SENT",
                "sent_at": f"2026-10-20T12:{i // 60:02}:{i % 60:02}+00:00",
                "subject": f"History subject {i:03}",
                "body": f"Saved sent body {i:03}",
            }
            db.execute(
                f"insert into messages ({','.join(row)}) values ({','.join('?' for _ in row)})",
                tuple(row.values()),
            )
    api = WebAPI(app.store.path.parent)
    page = api.post("/api/messages/sent", {"offset": 200}, "", "")
    assert page["total"] == 205
    assert len(page["messages"]) == 5
    assert page["messages"][-1]["id"] == "history-000"
    assert page["messages"][-1]["body"] == "Saved sent body 000"
    assert page["messages"][0]["email"] == seed.email
    assert page["messages"][0]["company"] == seed.company
    found = api.post("/api/messages/sent", {"search": "subject 000", "campaign_id": campaign.id}, "", "")
    assert found["total"] == 1 and found["messages"][0]["id"] == "history-000"
    assert api.post("/api/messages/sent", {"campaign_id": "missing"}, "", "")["total"] == 0
    assert api.post("/api/messages/sent", {"search": seed.email}, "", "")["total"] == 205


def test_send_progress_survives_reload_and_distinguishes_confirmed_from_unknown(setup, seed, monkeypatch):
    from pipeline.web_api import WebAPI
    from pipeline.models import now

    app, _, _, campaign = setup
    for i in range(3):
        app.prepare(
            campaign.id,
            seed.model_copy(update={"domain": f"progress{i}.org", "email": f"sam@progress{i}.org"}),
        )
    api = WebAPI(app.store.path.parent)
    monkeypatch.setattr(api, "app", lambda: app)
    monkeypatch.setattr(api, "readiness", lambda: {"ready_to_send": True})
    monkeypatch.setattr(api.worker, "start", lambda: {"running": True})
    result = api.post("/api/message/approve-all", {"confirm_us": True, "confirm_live_sending": True}, "", "")
    ids = result["approved"]
    app.store.execute("update messages set status='SENT',sent_at=? where id=?", (now().isoformat(), ids[0]))
    app.store.execute("update messages set status='UNKNOWN',error='AmbiguousSend' where id=?", (ids[1],))
    app.store.execute(
        "update jobs set error='Gmail rate limit; retry scheduled' where id=?", ("send:" + ids[2],)
    )
    progress = WebAPI(app.store.path.parent).delivery_status()
    assert progress["total"] == 3
    assert progress["counts"] == {"SENT": 1, "UNKNOWN": 1, "APPROVED": 1}
    assert progress["sent"][0]["email"].startswith("sam@progress")
    assert progress["sent"][0]["sent_at"]
    assert progress["waiting"][0]["error"] == "Gmail rate limit; retry scheduled"
    assert progress["attention"][0]["status"] == "UNKNOWN"


def test_snapshot_retains_recipient_of_older_pending_email(setup, seed):
    from pipeline.web_api import WebAPI

    app, _, _, campaign = setup
    original = app.prepare(campaign.id, seed)
    app.store.execute(
        "update contacts set updated_at='2000-01-01T00:00:00+00:00' where id=?", (original["id"],)
    )
    base = app.store.one("select * from contacts where id=?", (original["id"],))
    with app.store.tx() as db:
        for i in range(1001):
            row = {k: v for k, v in base.items() if k != "id"}
            row.update(email=f"newer{i}@example.org", updated_at="2026-10-21T00:00:00+00:00")
            db.execute(
                f"insert into contacts ({','.join(row)}) values ({','.join('?' for _ in row)})",
                tuple(row.values()),
            )
    snapshot = WebAPI(app.store.path.parent).snapshot()
    matches = [c for c in snapshot["contacts"] if c["id"] == original["id"]]
    assert len(matches) == 1 and matches[0]["email"] == seed.email


def test_single_approve_and_send_starts_worker(setup, seed, monkeypatch):
    from pipeline.web_api import WebAPI

    app, _, _, campaign = setup
    contact = app.prepare(campaign.id, seed)
    message = app.store.one("select id from messages where contact_id=?", (contact["id"],))
    api = WebAPI(app.store.path.parent)
    monkeypatch.setattr(api, "app", lambda: app)
    starts = []
    monkeypatch.setattr(api.worker, "start", lambda: starts.append(True) or {"running": True})
    result = api.post(
        "/api/message/approve",
        {"ids": [message["id"]], "confirm_us": True, "confirm_live_sending": True},
        "",
        "",
    )
    assert starts == [True]
    assert result["worker"]["running"]


def test_delivery_hides_obsolete_approval_error_and_past_retry_date(setup, seed, monkeypatch):
    from datetime import timedelta
    from pipeline.models import now
    from pipeline.web_api import WebAPI

    app, _, _, campaign = setup
    contact = app.prepare(campaign.id, seed)
    message = app.store.one("select id from messages where contact_id=?", (contact["id"],))
    app.approve(message["id"])
    app.store.enqueue("send:" + message["id"], "send", {"message_id": message["id"]})
    app.store.execute(
        "update jobs set error='Message requires approval',due_at=? where kind='send'",
        ((now() - timedelta(days=3)).isoformat(),),
    )
    api = WebAPI(app.store.path.parent)
    api.record_send_batch([message["id"]])
    waiting = api.delivery_status()["waiting"][0]
    assert waiting["error"] is None
    assert waiting["retry_at"] is None


@pytest.mark.parametrize("mode", ["REVIEW", "AUTONOMOUS"])
def test_manual_email_edit_persists_and_revokes_approval(setup, seed, monkeypatch, mode):
    from pipeline.web_api import WebAPI
    from pipeline.outbound import content_hash, Blocked
    import pytest

    app, model, mail, campaign = setup
    campaign.mode = type(campaign.mode)(mode)
    app.configure(campaign)
    app.store.execute("update campaigns set approved_hash=config_hash")
    app.prepare(campaign.id, seed)
    original = app.store.one("select * from messages limit 1")
    app.approve(original["id"])
    app.store.enqueue("send:" + original["id"], "send", {"message_id": original["id"]})
    api = WebAPI(app.store.path.parent)
    monkeypatch.setattr(api, "app", lambda: app)
    calls = len(model.calls)
    body = "\n" + original["body"].replace("Hi Sam,", "Hello Sam,")
    result = api.post("/api/message/edit", {
        "id": original["id"], "subject": "  A personal question  ", "body": body,
        "content_hash": original["content_hash"],
    }, "", "")
    edited = app.store.one("select * from messages where id=?", (original["id"],))
    assert result["saved"]
    assert edited["subject"] == "  A personal question  " and edited["body"] == body
    assert edited["status"] == "REVIEW" and edited["approved_hash"] is None
    assert edited["content_hash"] == content_hash(edited) != original["content_hash"]
    assert json.loads(edited["edit_audit"])["manual_edit"]
    assert len(model.calls) == calls and not mail.sent
    with pytest.raises(Blocked, match="approval"):
        app.send(edited["id"])
    app.approve(edited["id"])
    assert app.send(edited["id"]) == "SENT"
    assert mail.sent[0]["body"] == body


def test_manual_email_edit_rejects_stale_invalid_and_reserved_messages(setup, seed, monkeypatch):
    from pipeline.web_api import WebAPI
    import pytest

    app, model, mail, campaign = setup
    app.prepare(campaign.id, seed)
    original = app.store.one("select * from messages limit 1")
    api = WebAPI(app.store.path.parent)
    monkeypatch.setattr(api, "app", lambda: app)
    data = {"id": original["id"], "subject": original["subject"],
            "body": original["body"], "content_hash": original["content_hash"]}
    for patch in ({"subject": " "}, {"body": " "}, {"subject": "Hi\nBcc: bad@example.com"},
                  {"body": "No opt out"}, {"body": 123}, {"content_hash": "old"}):
        with pytest.raises(ValueError):
            api.post("/api/message/edit", {**data, **patch}, "", "")
    for status in ("SENDING", "UNKNOWN", "SENT", "CANCELLED", "FAILED"):
        app.store.execute("update messages set status=? where id=?", (status, original["id"]))
        with pytest.raises(ValueError):
            api.post("/api/message/edit", data, "", "")
        assert app.store.one("select body from messages where id=?", (original["id"],))["body"] == original["body"]
