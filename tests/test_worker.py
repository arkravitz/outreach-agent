from datetime import timedelta
from conftest import SKILL
from pipeline.worker import Runner
from pipeline.models import now
from pipeline.outbound import SafeRetry
from pipeline.service import Pipeline
from test_outbound import prepared
from test_replies import sent, event


def test_restart_resumes_failed_preparation_without_duplicate_draft(setup, seed):
    app, model, mail, campaign = setup
    app.writer.skill_dir = app.writer.skill_dir / "missing"
    try:
        app.prepare(campaign.id, seed)
    except ValueError:
        pass
    app = Pipeline(app.store, model, mail, skill_dir=SKILL)
    contact = app.store.one("select id from contacts")
    Runner(app).run("prepare:" + str(contact["id"]))
    assert app.store.one("select count(*) n from messages")["n"] == 1
    assert app.store.one("select state from contacts")["state"] == "READY_FOR_OUTREACH"


def test_due_followup_drafted_once_and_cancelled_by_reply(setup, seed):
    app, _, contact, _ = sent(setup, seed)
    app.store.execute("update followups set due_at=?", ((now() - timedelta(minutes=1)).isoformat(),))
    runner = Runner(app)
    runner.tick()
    runner.tick()
    job = app.store.one("select id from jobs where kind='followup'")
    runner.run(job["id"])
    runner.run(job["id"])
    assert app.store.one("select count(*) n from messages where category='followup'")["n"] == 1
    app.ingest(event(contact, "Not interested"))
    assert app.store.one("select status from messages where category='followup'")["status"] == "CANCELLED"


def test_rate_limit_backoff_recovers_without_manual_replay(setup, seed):
    app, mail, contact, message = prepared(setup, seed)
    app.approve(message["id"])
    app.store.enqueue("send-test", "send", {"message_id": message["id"]})
    mail.error = SafeRetry("rate limit")
    runner = Runner(app)
    for attempt in range(5):
        app.store.execute("delete from sync_state where key='gmail_send_not_before'")
        app.store.execute(
            "update jobs set due_at=?,status='PENDING' where id='send-test'", (now().isoformat(),)
        )
        runner.run("send-test")
    job = app.store.one("select * from jobs where id='send-test'")
    assert job["status"] == "PENDING"
    assert job["attempts"] == 5
    assert not mail.sent
    app.store.execute("delete from sync_state where key='gmail_send_not_before'")
    app.store.execute("update jobs set due_at=? where id='send-test'", (now().isoformat(),))
    mail.error = None
    runner.run("send-test")
    assert app.store.one("select status from jobs where id='send-test'")["status"] == "DONE"
    assert len(mail.sent) == 1


def test_expired_job_lease_recovers_after_worker_crash(setup, seed):
    app, mail, contact, message = prepared(setup, seed)
    app.approve(message["id"])
    app.store.enqueue("crashed", "send", {"message_id": message["id"]})
    app.store.execute(
        "update jobs set status='RUNNING',lease_until=? where id='crashed'",
        ((now() - timedelta(minutes=1)).isoformat(),),
    )
    runner = Runner(app)
    runner.tick()
    runner.run("crashed")
    assert len(mail.sent) == 1


def test_manual_takeover_stops_followups(setup, seed):
    app, mail, contact, _ = sent(setup, seed)
    app.store.execute("update contacts set takeover=1 where id=?", (contact["id"],))
    app.store.execute("update followups set due_at=?", (now().isoformat(),))
    runner = Runner(app)
    runner.tick()
    job = app.store.one("select id from jobs where kind='followup'")
    if job:
        runner.run(job["id"])
    assert app.store.one("select count(*) n from messages where category='followup'")["n"] == 0


def test_followup_cannot_repeat_previous_email_verbatim(setup, seed):
    from pipeline.writing import WritingError
    import pytest

    app, _, contact, _ = sent(setup, seed)
    model = app.model
    original = model.complete

    def repeat(task, payload):
        if task == "write" and payload.get("category") == "followup":
            payload = {**payload, "category": "initial"}
        return original(task, payload)

    model.complete = repeat
    with pytest.raises(WritingError, match="repeat"):
        Runner(app).followup(app.store.one("select id from followups")["id"])


def test_concurrent_preparation_does_not_duplicate_model_calls(setup, seed):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    app, model, _, _ = setup
    entered, release = threading.Event(), threading.Event()
    original = model.complete

    def blocking(task, payload):
        if task == "qualify" and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(task, payload)

    model.complete = blocking
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(app.prepare, "automation", seed)
        assert entered.wait(5)
        contact = app.store.one("select id from contacts")
        Runner(app).run("prepare:" + str(contact["id"]))
        release.set()
        future.result(timeout=5)
    assert len([t for t, p in model.calls if t == "qualify"]) == 1
    assert app.store.one("select count(*) n from messages")["n"] == 1


def test_exited_owned_worker_is_not_misidentified_as_external(setup):
    from pipeline.worker_control import WorkerControl

    app, _, _, _ = setup

    class Exited:
        def poll(self):
            return 0

    control = WorkerControl(app.store.path.parent, app.store)
    control.process = Exited()
    app.store.execute("insert into sync_state values ('worker_heartbeat',?)", (now().isoformat(),))
    assert control.status()["running"] is False
    assert control.status()["external"] is False


def test_automatic_draft_daily_budget_waits_instead_of_deadletter(setup, seed):
    app, model, _, campaign = setup
    campaign.prospecting_goal = "RELATIONSHIPS"
    app.configure(campaign)
    app.prepare(campaign.id, seed)
    campaign.auto_draft = True
    app.configure(campaign)
    app.queue_automatic_drafts()
    job = app.store.one("select id from jobs where id like 'auto-draft:%'")
    original = model.complete

    def limited(task, payload):
        if task == "write":
            raise SafeRetry("Model daily call budget reached")
        return original(task, payload)

    model.complete = limited
    for _ in range(6):
        app.store.execute("update jobs set due_at=? where id=?", (now().isoformat(), job["id"]))
        Runner(app).run(job["id"])
    assert app.store.one("select status from jobs where id=?", (job["id"],))["status"] == "PENDING"
    assert not app.store.all("select * from escalations")


def test_automatic_regeneration_replaces_stale_review_once(setup, seed):
    app, _, mail, campaign = setup
    app.prepare(campaign.id, seed)
    campaign.prospecting_goal = "RELATIONSHIPS"
    campaign.auto_draft = True
    app.configure(campaign)
    assert app.queue_automatic_drafts() == 1
    job = app.store.one("select * from jobs where id like 'auto-draft:%'")
    assert job["kind"] == "redraft"
    Runner(app).run(job["id"])
    assert len(app.store.all("select * from messages where status='REVIEW'")) == 1
    assert app.queue_automatic_drafts() == 0
    assert not mail.sent


def test_automatic_regeneration_obeys_disabled_setting(setup, seed):
    app, _, _, campaign = setup
    app.prepare(campaign.id, seed)
    campaign.prospecting_goal = "RELATIONSHIPS"
    campaign.auto_draft = True
    app.configure(campaign)
    app.queue_automatic_drafts()
    job = app.store.one("select * from jobs where kind='redraft'")
    campaign.auto_draft = False
    app.configure(campaign)
    Runner(app).run(job["id"])
    assert app.store.one("select count(*) n from messages")["n"] == 1


def test_discard_during_automatic_regeneration_is_not_resurrected(setup, seed):
    app, _, _, campaign = setup
    app.prepare(campaign.id, seed)
    campaign.prospecting_goal = "RELATIONSHIPS"
    campaign.auto_draft = True
    app.configure(campaign)
    app.queue_automatic_drafts()
    job = app.store.one("select * from jobs where kind='redraft'")
    original = app.writer.compose

    def discard(*args, **kwargs):
        result = original(*args, **kwargs)
        app.store.execute("update messages set status='CANCELLED'")
        return result

    app.writer.compose = discard
    Runner(app).run(job["id"])
    assert not app.store.all("select * from messages where status='REVIEW'")


def test_discovery_waits_for_due_automatic_draft_backlog(setup, seed, monkeypatch):
    import pipeline.research as research

    app, _, _, campaign = setup
    campaign.prospecting_goal = "RELATIONSHIPS"
    app.configure(campaign)
    app.prepare(campaign.id, seed)
    campaign.auto_draft = True
    app.configure(campaign)
    app.queue_automatic_drafts()
    app.store.enqueue("test-discovery-backlog", "discover", {"campaign_id": campaign.id})
    monkeypatch.setattr(
        research, "discover", lambda *args: (_ for _ in ()).throw(AssertionError("Draft first"))
    )
    Runner(app).run("test-discovery-backlog")
    row = app.store.one("select status,error from jobs where id='test-discovery-backlog'")
    assert row["status"] == "PENDING" and "draft" in row["error"].lower()


def test_send_rate_limit_retries_beyond_five_and_cools_down_other_mail(setup, seed):
    from datetime import datetime
    from pipeline.outbound import Blocked
    import pytest

    app, mail, _, message = prepared(setup, seed)
    app.approve(message["id"])
    app.store.enqueue("rate-limited", "send", {"message_id": message["id"]})
    mail.error = SafeRetry("Gmail rate limit", retry_after_seconds=7200)
    runner = Runner(app)
    for i in range(6):
        app.store.execute("delete from sync_state where key='gmail_send_not_before'")
        app.store.execute("update jobs set due_at=? where id='rate-limited'", (now().isoformat(),))
        runner.run("rate-limited")
    job = app.store.one("select * from jobs where id='rate-limited'")
    assert job["status"] == "PENDING"
    assert datetime.fromisoformat(job["due_at"]) > now() + timedelta(seconds=7100)
    assert app.store.one("select status from messages where id=?", (message["id"],))["status"] == "APPROVED"
    with pytest.raises(Blocked, match="cooldown"):
        app.send(message["id"])
    app.store.execute("delete from sync_state where key='gmail_send_not_before'")
    app.store.execute("update jobs set due_at=? where id='rate-limited'", (now().isoformat(),))
    mail.error = None
    runner.run("rate-limited")
    assert len(mail.sent) == 1
    assert app.store.one("select status from jobs where id='rate-limited'")["status"] == "DONE"
