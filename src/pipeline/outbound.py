"""Serialized final send gates with fail-closed ambiguity handling."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json
import random
from contextlib import nullcontext
from .models import Seed, digest, now


class Blocked(ValueError):
    pass


class SafeRetry(Exception):
    """Provider explicitly rejected the request before acceptance (e.g. HTTP 429)."""

    def __init__(self, message, retry_after_seconds=None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class AmbiguousSend(Exception):
    """Request may have been accepted. Must reconcile; never automatically resend."""


def business_days(start, days, zone="America/Los_Angeles"):
    local = start.astimezone(ZoneInfo(zone))
    for _ in range(days):
        local += timedelta(days=1)
        while local.weekday() >= 5:
            local += timedelta(days=1)
    return local.astimezone(start.tzinfo)


def content_hash(message):
    return digest(
        {
            "body": message["body"],
            "subject": message["subject"],
            "config": message["config_hash"],
            "category": message["category"],
            "reply_to": message["reply_to"],
        }
    )


def edit(app, message_id, subject, body, expected_hash):
    from .writing import OPT_OUT, LEGACY_OPT_OUT

    if not isinstance(subject, str) or not subject.strip() or any(c in subject for c in "\r\n\x00"):
        raise Blocked("Enter a subject on one line")
    if not isinstance(body, str) or not body.strip() or "\x00" in body:
        raise Blocked("Enter an email body")
    if not any(text in body for text in (OPT_OUT, LEGACY_OPT_OUT)):
        raise Blocked("Keep the opt-out sentence in the email body")
    app.writer.check_profile(subject, body)
    with app.store.tx() as db:
        row = db.execute("select * from messages where id=?", (message_id,)).fetchone()
        if not row or row["status"] not in {"REVIEW", "APPROVED", "DRY_RUN"}:
            raise Blocked("This email is no longer editable; refresh to check its status")
        if row["content_hash"] != expected_hash or row["content_hash"] != content_hash(row):
            raise Blocked("This draft changed since you opened it. Reopen it before editing")
        message = dict(row)
        message.update(subject=subject, body=body)
        audit = json.loads(row["edit_audit"])
        audit["manual_edit"] = True
        audit["manual_edited_at"] = now().isoformat()
        db.execute(
            "update messages set subject=?,body=?,content_hash=?,approved_hash=NULL,"
            "status='REVIEW',edit_audit=?,error=NULL where id=?",
            (message["subject"], body, content_hash(message), json.dumps(audit), message_id),
        )
        db.execute(
            "update jobs set status='CANCELLED',lease_until=NULL where id=? and status='PENDING'",
            ("send:" + message_id,),
        )
        app.store.audit("message.manually_edited", message_id,
                        {"previous_hash": expected_hash, "hash": content_hash(message)}, db)
    return message_id


def approve(app, message_id, *, confirm_us=False, legal_basis=None, provenance=None, _db=None):
    with app.store.tx() if _db is None else nullcontext(_db) as db:
        row = db.execute("select * from messages where id=?", (message_id,)).fetchone()
        if not row or row["status"] not in {"REVIEW", "APPROVED"}:
            raise Blocked("Message is not awaiting approval")
        if row["content_hash"] != content_hash(row):
            raise Blocked("Message content changed; regenerate it")
        campaign = db.execute("select * from campaigns where id=?", (row["campaign_id"],)).fetchone()
        if row["config_hash"] != campaign["config_hash"]:
            raise Blocked("Campaign configuration changed; regenerate the message")
        contact = db.execute("select * from contacts where id=?", (row["contact_id"],)).fetchone()
        seed = Seed.model_validate_json(contact["seed"])
        if confirm_us:
            seed.region, seed.region_reviewed = "US", True
            if legal_basis and legal_basis.strip():
                seed.legal_basis = legal_basis.strip()
            if provenance and provenance.strip():
                seed.provenance = provenance.strip()
            db.execute("update contacts set seed=? where id=?", (seed.model_dump_json(), contact["id"]))
            app.store.audit("contact.reviewed", contact["id"], db=db)
        if seed.region != "US" or not seed.region_reviewed:
            raise Blocked("Confirm the recipient is in the US before approving")
        db.execute(
            "update messages set status='APPROVED',approved_hash=content_hash where id=?", (message_id,)
        )
        due = max(now(), datetime.fromisoformat(row["due_at"]))
        cooldown = db.execute("select value from sync_state where key='gmail_send_not_before'").fetchone()
        if cooldown:
            due = max(due, datetime.fromisoformat(cooldown[0]))
        db.execute(
            "update jobs set status='PENDING',due_at=?,error=NULL,lease_until=NULL "
            "where id=? and status in ('PENDING','DONE','DEAD','CANCELLED')",
            (due.isoformat(), "send:" + message_id),
        )
        app.store.audit("message.approved", message_id, {"hash": row["content_hash"]}, db)
    return message_id


def _gates(app, db, message, contact, campaign, config, time):
    from .writing import WritingError

    try:
        app.writer.check_profile(message["subject"], message["body"])
    except WritingError:
        raise Blocked("Email conflicts with saved profile preferences; regenerate before sending") from None
    if (
        db.execute("select value from settings where key='paused'").fetchone()[0] == "true"
        or config["paused"]
    ):
        raise Blocked("Sending is paused")
    cooldown = db.execute("select value from sync_state where key='gmail_send_not_before'").fetchone()
    if cooldown and datetime.fromisoformat(cooldown[0]) > time:
        raise Blocked("Gmail rate-limit cooldown; sending will resume automatically")
    if campaign.mode == "DRY_RUN":
        raise Blocked("DRY_RUN cannot send")
    suppressed = db.execute(
        "select 1 from suppressions where email=? or company_id=?", (contact["email"], contact["company_id"])
    ).fetchone()
    if suppressed or contact["invalid"] or contact["state"] in {"SUPPRESSED", "BOUNCED", "LOST", "REJECTED"}:
        raise Blocked("Contact is suppressed, invalid or closed")
    conversational = message["category"] in {
        "factual_answer",
        "scheduling",
        "proposal",
        "testimonial",
        "referral",
    }
    if (contact["reply_hold"] and not conversational) or contact["takeover"]:
        raise Blocked("Conversation requires review or manual takeover")
    if contact["state"] == "MEETING_BOOKED" and message["category"] in {"initial", "followup"}:
        raise Blocked("Meeting already booked")
    if message["config_hash"] != config["config_hash"]:
        raise Blocked("Campaign configuration changed")
    if message["content_hash"] != content_hash(message):
        raise Blocked("Message content changed")
    if (
        message["approved_hash"] != message["content_hash"]
        and db.execute(
            "select 1 from audit where event in ('message.opt_out_updated','message.manually_edited') "
            "and entity=? limit 1",
            (message["id"],),
        ).fetchone()
    ):
        raise Blocked("Updated email requires approval")
    if campaign.mode == "AUTONOMOUS":
        if config["approved_hash"] != config["config_hash"]:
            raise Blocked("Autonomous campaign authorization missing or obsolete")
        if (
            message["category"] not in campaign.autonomous_categories
            and message["approved_hash"] != message["content_hash"]
        ):
            raise Blocked("Message category requires user approval")
    elif message["approved_hash"] != message["content_hash"]:
        raise Blocked("Message requires approval")
    seed = Seed.model_validate_json(contact["seed"])
    if seed.region not in campaign.allowed_regions or not seed.region_reviewed:
        raise Blocked("Recipient region has not been reviewed or is unsupported")
    if seed.verification not in {"published", "verified"}:
        raise Blocked("Published or verified address required")
    if contact["domain"] in campaign.blocked_domains:
        raise Blocked("Domain blocked by campaign policy")
    if not all((campaign.sender_name.strip(), campaign.sender_email)):
        raise Blocked("Sender name and email required")
    from .writing import OPT_OUT, LEGACY_OPT_OUT

    if not message["skill_hash"] or not any(text in message["body"] for text in (OPT_OUT, LEGACY_OPT_OUT)):
        raise Blocked("Mandatory editor and opt-out missing")
    if datetime.fromisoformat(message["due_at"]) > time:
        raise Blocked("Message not due")
    day = time.astimezone(ZoneInfo(campaign.timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
    day = day.astimezone(time.tzinfo).isoformat()
    counts = db.execute(
        """select m.campaign_id,c.domain from messages m join contacts c on c.id=m.contact_id
        where m.sent_at>=? and m.status in ('SENT','SENDING','UNKNOWN')""",
        (day,),
    ).fetchall()
    # Enforce the most conservative configured account-wide limit across all campaigns.
    all_configs = db.execute("select config from campaigns").fetchall()
    global_limit = min(json.loads(r[0])["global_daily_limit"] for r in all_configs)
    if (
        len(counts) >= global_limit
        or sum(r["campaign_id"] == campaign.id for r in counts) >= campaign.daily_limit
    ):
        raise Blocked("Daily sending limit reached")
    if sum(r["domain"] == contact["domain"] for r in counts) >= campaign.domain_daily_limit:
        raise Blocked("Domain daily limit reached")
    latest = db.execute(
        "select max(sent_at) from messages where status in ('SENT','SENDING','UNKNOWN')"
    ).fetchone()[0]
    if (
        latest
        and (time - datetime.fromisoformat(latest)).total_seconds() < campaign.min_send_interval_seconds
    ):
        raise Blocked("Provider sending rate limit reached")


def _record_sent(app, db, message, contact, result, timestamp):
    if not result.get("id") or not result.get("threadId"):
        raise AmbiguousSend("Provider response lacks identifiers")
    db.execute(
        "update messages set status='SENT',gmail_id=?,thread_id=?,error=NULL where id=?",
        (result["id"], result["threadId"], message["id"]),
    )
    db.execute("update contacts set thread_id=? where id=?", (result["threadId"], contact["id"]))
    if message["category"] == "initial":
        app.transition(contact["id"], "AWAITING_REPLY", db)
    if message["category"] == "followup":
        db.execute(
            "update followups set status='SENT' where contact_id=? and ordinal=?",
            (contact["id"], contact["followup_count"]),
        )
    if message["category"] in {"initial", "followup"} and not contact["reply_hold"]:
        campaign = app.campaign(contact["campaign_id"])
        ordinal = contact["followup_count"] + 1
        if ordinal <= len(campaign.followup_business_days):
            due = business_days(timestamp, campaign.followup_business_days[ordinal - 1], campaign.timezone)
            db.execute(
                "insert or ignore into followups(contact_id,ordinal,due_at,status) values (?,?,?,'PENDING')",
                (contact["id"], ordinal, due.isoformat()),
            )
    app.store.audit("message.sent", message["id"], {"gmail_id": result["id"]}, db)


def send(app, message_id):
    # Reserve durably BEFORE calling Gmail. A process death now cannot trigger a second send.
    with app.store.tx() as db:
        row = db.execute("select * from messages where id=?", (message_id,)).fetchone()
        if not row:
            raise Blocked("Unknown message")
        if row["status"] in {"SENT", "SENDING", "UNKNOWN", "CANCELLED", "FAILED"}:
            return row["status"]
        contact = db.execute("select * from contacts where id=?", (row["contact_id"],)).fetchone()
        config = db.execute("select * from campaigns where id=?", (row["campaign_id"],)).fetchone()
        campaign = app.campaign(row["campaign_id"])
        _gates(app, db, row, contact, campaign, config, now())
        db.execute(
            "update messages set status='SENDING',sent_at=?,attempts=attempts+1 where id=?",
            (now().isoformat(), message_id),
        )
    # Hold the same SQLite writer gate used by ingest, pause and suppression while the bounded
    # provider call runs. Recheck all mutable stop conditions after reservation.
    with app.store.tx() as db:
        message = dict(db.execute("select * from messages where id=?", (message_id,)).fetchone())
        if message["status"] != "SENDING":
            return message["status"]
        contact = dict(db.execute("select * from contacts where id=?", (message["contact_id"],)).fetchone())
        paused = db.execute("select value from settings where key='paused'").fetchone()[0] == "true"
        campaign_row = db.execute("select * from campaigns where id=?", (message["campaign_id"],)).fetchone()
        stopped = (
            paused
            or campaign_row["paused"]
            or contact["invalid"]
            or contact["takeover"]
            or campaign_row["config_hash"] != message["config_hash"]
            or (contact["reply_hold"] and message["category"] in {"initial", "followup"})
            or db.execute(
                "select 1 from suppressions where email=? or company_id=?",
                (contact["email"], contact["company_id"]),
            ).fetchone()
        )
        if stopped:
            db.execute("update messages set status='CANCELLED',sent_at=NULL where id=?", (message_id,))
            return "CANCELLED"
        message.update(
            {
                "to": contact["email"],
                "from": campaign.sender_email,
                "from_name": campaign.sender_name,
                "thread_id": contact["thread_id"],
            }
        )
        try:
            cooldown = db.execute("select value from sync_state where key='gmail_send_not_before'").fetchone()
            if cooldown and datetime.fromisoformat(cooldown[0]) > now():
                raise SafeRetry(
                    "Gmail rate-limit cooldown",
                    retry_after_seconds=(datetime.fromisoformat(cooldown[0]) - now()).total_seconds(),
                )
            result = app.mail.send(message)
            _record_sent(app, db, message, contact, result, now())
        except SafeRetry as exc:
            # Persist the account cooldown before releasing the serialized provider-call lock.
            delay = max(
                min(3600, 30 * 2 ** min(message["attempts"] - 1, 7)) + random.uniform(0, 30),
                exc.retry_after_seconds or 0,
            )
            due = now() + timedelta(seconds=delay)
            existing = db.execute("select value from sync_state where key='gmail_send_not_before'").fetchone()
            if existing:
                due = max(due, datetime.fromisoformat(existing[0]))
            db.execute(
                "insert into sync_state values ('gmail_send_not_before',?) "
                "on conflict(key) do update set value=excluded.value",
                (due.isoformat(),),
            )
            retry_error = SafeRetry(str(exc), retry_after_seconds=(due - now()).total_seconds())
            db.execute(
                "update messages set status='APPROVED',sent_at=NULL,error='SafeRetry' where id=?",
                (message_id,),
            )
            outcome = "RETRY"
        except Exception as exc:
            db.execute(
                "update messages set status='UNKNOWN',error=? where id=?", (type(exc).__name__, message_id)
            )
            app.store.escalate(
                contact["id"], f"Ambiguous send {message_id}: reconcile; never resend blindly", db
            )
            app.store.audit("message.unknown", message_id, db=db)
            outcome = "UNKNOWN"
        else:
            outcome = "SENT"
    if outcome == "RETRY":
        raise retry_error
    return outcome


def reconcile(app, message_id):
    message = app.store.one("select * from messages where id=?", (message_id,))
    if not message or message["status"] not in {"SENDING", "UNKNOWN"}:
        raise Blocked("Only ambiguous sends can be reconciled")
    result = app.mail.find_sent(message["rfc_id"])
    with app.store.tx() as db:
        message = db.execute("select * from messages where id=?", (message_id,)).fetchone()
        if message["status"] == "SENT":
            return "SENT"
        if result:
            contact = db.execute("select * from contacts where id=?", (message["contact_id"],)).fetchone()
            _record_sent(app, db, message, contact, result, datetime.fromisoformat(message["sent_at"]))
            return "SENT"
        db.execute("update messages set status='UNKNOWN' where id=?", (message_id,))
        return "UNKNOWN"  # Search absence is NOT proof Gmail did not accept it.
