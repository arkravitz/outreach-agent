"""Huey handles worker wakeups; SQLite outbox closes crash/queue transaction gaps."""

from datetime import datetime, timedelta
import json
import random
import os
from pathlib import Path
from huey import SqliteHuey, crontab
from .models import Seed, now
from .outbound import Blocked, SafeRetry


class Runner:
    def __init__(self, app, sync=None):
        self.app = app
        self.store = app.store
        self.sync = sync

    def tick(self):
        timestamp = now()
        if self.store.one("select value from settings where key='paused'")["value"] != "true":
            self.app.queue_automatic_drafts()
        with self.store.tx() as db:
            db.execute(
                "update jobs set status='PENDING',lease_until=NULL where status='RUNNING' and lease_until<?",
                (timestamp.isoformat(),),
            )
            for row in db.execute(
                "select * from followups where status='PENDING' and due_at<=?", (timestamp.isoformat(),)
            ).fetchall():
                self.store.enqueue(
                    f"followup:{row['contact_id']}:{row['ordinal']}",
                    "followup",
                    {"followup_id": row["id"]},
                    db=db,
                )
            for row in db.execute(
                "select id from messages where status in ('APPROVED','REVIEW') and due_at<=?",
                (timestamp.isoformat(),),
            ).fetchall():
                self.store.enqueue("send:" + row["id"], "send", {"message_id": row["id"]}, db=db)
            if self.sync:
                # Minute key gives a durable checkpointed poll, independent of any interactive session.
                self.store.enqueue("poll:" + timestamp.strftime("%Y%m%d%H%M"), "poll", {}, db=db)
            global_paused = (
                db.execute("select value from settings where key='paused'").fetchone()[0] == "true"
            )
            from .settings import SettingsStore

            discovery_settings = SettingsStore(self.store.path.parent).load()
            discovery_enabled = discovery_settings.discovery_enabled
            if not global_paused and discovery_enabled:
                for campaign in db.execute("select id,config from campaigns where paused=0").fetchall():
                    if json.loads(campaign["config"]).get("discovery_queries"):
                        self.store.enqueue(
                            "discover:"
                            + campaign["id"]
                            + ":"
                            + timestamp.strftime("%Y%m%d")
                            + ":"
                            + str(timestamp.hour // discovery_settings.discovery_interval_hours),
                            "discover",
                            {"campaign_id": campaign["id"]},
                            db=db,
                        )
            self.store.enqueue("digest:" + timestamp.strftime("%Y%m%d"), "digest", {}, db=db)
        return self.store.all(
            """select id,due_at from jobs where status='PENDING' and due_at<=?
            order by case when kind in ('poll','classify') then 0 when kind='send' then 1
            when kind in ('prepare','redraft') then 2 else 3 end,due_at""",
            (timestamp.isoformat(),),
        )

    def run(self, job_id):
        with self.store.tx() as db:
            job = db.execute("select * from jobs where id=?", (job_id,)).fetchone()
            if not job or job["status"] != "PENDING" or datetime.fromisoformat(job["due_at"]) > now():
                return
            db.execute(
                "update jobs set status='RUNNING',attempts=attempts+1,lease_until=? where id=?",
                ((now() + timedelta(minutes=15)).isoformat(), job_id),
            )
            payload = json.loads(job["payload"])
            kind = job["kind"]
        try:
            if kind == "prepare":
                self.app.resume(payload["contact_id"])
            elif kind == "redraft":
                contact = self.store.one("select * from contacts where id=?", (payload["contact_id"],))
                campaign = self.store.one(
                    "select config_hash,paused from campaigns where id=?", (contact["campaign_id"],)
                )
                current = self.store.one(
                    "select id,config_hash,status from messages where contact_id=? and category='initial' and status!='CANCELLED' order by created_at desc limit 1",
                    (contact["id"],),
                )
                policy = self.app.campaign(contact["campaign_id"])
                if (
                    current
                    and policy.auto_draft
                    and policy.prospecting_goal == "RELATIONSHIPS"
                    and not campaign["paused"]
                    and (contact["score"] or 0) >= policy.auto_draft_threshold
                    and current["status"] in {"REVIEW", "APPROVED", "DRY_RUN"}
                    and current["config_hash"] != campaign["config_hash"]
                    and self.app.draftable_contact(contact, self.app.campaign(contact["campaign_id"]))
                ):
                    self.app.redraft(contact["id"], expected_message_id=current["id"])
            elif kind == "send":
                self.app.send(payload["message_id"])
            elif kind == "classify":
                self.app.classify(payload["inbound_id"])
            elif kind == "followup":
                self.followup(payload["followup_id"])
            elif kind == "poll":
                if self.sync:
                    self.sync.poll()
            elif kind == "discover":
                if self.store.one(
                    "select id from jobs where id like 'auto-draft:%' and status in ('PENDING','RUNNING') and due_at<=? limit 1",
                    (now().isoformat(),),
                ):
                    raise Blocked("Waiting for queued email drafts before further discovery")
                from .research import discover

                discover(self.app, payload["campaign_id"])
            elif kind == "digest":
                from .crm import digest_report

                report = digest_report(self.app)
                self.store.execute(
                    "insert into sync_state values ('daily_digest',?) on conflict(key) do update set value=excluded.value",
                    (json.dumps(report),),
                )
            else:
                raise ValueError("Unknown job kind")
        except Blocked as exc:
            # Approval/pause/rate gates are expected, not dead-letter failures.
            self.store.execute(
                "update jobs set status='PENDING',due_at=?,lease_until=NULL,error=? where id=?",
                ((now() + timedelta(minutes=5)).isoformat(), str(exc), job_id),
            )
        except Exception as exc:
            if isinstance(exc, SafeRetry) and kind == "send":
                attempts = self.store.one("select attempts from jobs where id=?", (job_id,))["attempts"]
                delay = max(
                    min(3600, 30 * 2 ** min(attempts - 1, 7)) + random.uniform(0, 30),
                    exc.retry_after_seconds or 0,
                )
                due = now() + timedelta(seconds=delay)
                with self.store.tx() as db:
                    existing = db.execute(
                        "select value from sync_state where key='gmail_send_not_before'"
                    ).fetchone()
                    if existing:
                        due = max(due, datetime.fromisoformat(existing[0]))
                    db.execute(
                        "insert into sync_state values ('gmail_send_not_before',?) "
                        "on conflict(key) do update set value=excluded.value",
                        (due.isoformat(),),
                    )
                    db.execute(
                        "update jobs set status='PENDING',due_at=?,lease_until=NULL,error=? where id=?",
                        (due.isoformat(), "Gmail rate limit; retry scheduled", job_id),
                    )
                    db.execute(
                        "update jobs set due_at=? where kind='send' and status='PENDING' and due_at<?",
                        (due.isoformat(), due.isoformat()),
                    )
                    self.store.audit("send.rate_limited", job_id, {"retry_at": due.isoformat()}, db)
                return
            if isinstance(exc, SafeRetry) and kind in {"prepare", "redraft"}:
                due = now() + timedelta(minutes=1)
                if "daily" in str(exc).lower():
                    due = (now() + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
                self.store.execute(
                    "update jobs set status='PENDING',due_at=?,lease_until=NULL,error=? where id=?",
                    (due.isoformat(), str(exc)[:200], job_id),
                )
                return
            with self.store.tx() as db:
                attempts = db.execute("select attempts from jobs where id=?", (job_id,)).fetchone()[0]
                status = "DEAD" if attempts >= 5 else "PENDING"
                db.execute(
                    "update jobs set status=?,due_at=?,lease_until=NULL,error=? where id=?",
                    (
                        status,
                        (now() + timedelta(seconds=30 * 2 ** min(attempts - 1, 8))).isoformat(),
                        type(exc).__name__,
                        job_id,
                    ),
                )
                self.store.audit(
                    "workflow.failure", job_id, {"error": type(exc).__name__, "attempt": attempts}, db
                )
                if status == "DEAD":
                    self.store.escalate(None, f"Workflow {job_id} exhausted retries", db)
        else:
            self.store.execute(
                "update jobs set status='DONE',lease_until=NULL,error=NULL where id=?", (job_id,)
            )

    def followup(self, followup_id):
        row = self.store.one("select * from followups where id=?", (followup_id,))
        if not row or row["status"] != "PENDING":
            return
        contact = self.store.one("select * from contacts where id=?", (row["contact_id"],))
        if (
            contact["reply_hold"]
            or contact["takeover"]
            or contact["invalid"]
            or contact["state"] != "AWAITING_REPLY"
        ):
            self.store.execute("update followups set status='CANCELLED' where id=?", (followup_id,))
            return
        campaign = self.app.campaign(contact["campaign_id"])
        if row["ordinal"] > len(campaign.followup_business_days) or row["ordinal"] > 2:
            return
        previous = self.store.all(
            "select subject,body,rfc_id from messages where contact_id=? and status='SENT' order by sent_at",
            (contact["id"],),
        )
        content = self.app.writer.compose(
            campaign,
            Seed.model_validate_json(contact["seed"]),
            "followup",
            {
                "previous_messages": previous,
                "ordinal": row["ordinal"],
                "thread_subject": previous[0]["subject"] if previous else None,
                "instruction": "Add new useful context grounded in evidence. Do not repeat or merely check in.",
            },
        )
        with self.store.tx() as db:
            current = db.execute("select * from contacts where id=?", (contact["id"],)).fetchone()
            follow = db.execute("select status from followups where id=?", (followup_id,)).fetchone()
            if follow["status"] != "PENDING" or current["reply_hold"] or current["takeover"]:
                return
            self.app.queue_message(
                contact["id"],
                content,
                "followup",
                db=db,
                reply_to=previous[-1]["rfc_id"] if previous else None,
            )
            db.execute("update followups set status='QUEUED' where id=?", (followup_id,))
            db.execute(
                "update contacts set followup_count=max(followup_count,?) where id=?",
                (row["ordinal"], contact["id"]),
            )

    def replay(self, job_id):
        with self.store.tx() as db:
            job = db.execute("select * from jobs where id=?", (job_id,)).fetchone()
            if not job or job["status"] not in {"DEAD", "CANCELLED"}:
                raise ValueError("Only dead or cancelled workflows can be replayed")
            db.execute(
                "update jobs set status='PENDING',attempts=0,due_at=?,lease_until=NULL where id=?",
                (now().isoformat(), job_id),
            )
            self.store.audit("workflow.replayed", job_id, db=db)

    def cancel(self, job_id):
        self.store.execute(
            "update jobs set status='CANCELLED' where id=? and status in ('PENDING','RUNNING')", (job_id,)
        )


# Lazy construction avoids opening provider accounts during import/testing.
_state = Path(os.environ.get("PIPELINE_STATE", "state")).resolve()
_state.mkdir(exist_ok=True, parents=True, mode=0o700)
huey = SqliteHuey("pipeline", filename=str(_state / "queue.db"), results=False)


@huey.task(retries=2, retry_delay=30, retry_backoff=2, timeout=600)
def execute_job(job_id):
    from .runtime import build
    from .gmail import GmailSync

    app = build()
    sync = GmailSync(app, app.mail) if Path(app.mail.token_file).exists() else None
    Runner(app, sync).run(job_id)


@huey.periodic_task(crontab(minute="*"))
def wake():
    from .runtime import build
    from .gmail import GmailSync

    app = build()
    app.store.execute(
        "insert into sync_state values ('worker_heartbeat',?) on conflict(key) do update set value=excluded.value",
        (now().isoformat(),),
    )
    sync = GmailSync(app, app.mail) if Path(app.mail.token_file).exists() else None
    for job in Runner(app, sync).tick():
        execute_job(job["id"])
