"""Incoming events stop follow-ups transactionally before any model call."""

from email.utils import parseaddr
import re
from .models import Classification, now

CATEGORIES = {
    "INTERESTED",
    "QUESTION",
    "NOT_INTERESTED",
    "UNSUBSCRIBE",
    "OUT_OF_OFFICE",
    "BOUNCE",
    "SCHEDULING",
    "UNCERTAIN",
}


def new_text(body):
    lines = []
    for line in body.splitlines():
        if re.match(r"(?i)^\s*(On .+wrote:|From:|-----Original Message-----)", line):
            break
        if not line.lstrip().startswith(">"):
            lines.append(line)
    return "\n".join(lines).strip()


def deterministic(body, bounce=False):
    text = new_text(body).lower()
    if re.search(r"\b(unsubscribe|remove me|stop (emailing|contacting)|do not (email|contact))\b", text):
        return Classification(category="UNSUBSCRIBE", confidence=1, reason="Explicit opt-out")
    if bounce:
        return Classification(
            category="BOUNCE", confidence=1, reason="Validated delivery status notification"
        )
    if re.search(r"\b(not interested|no thanks|no thank you|not a fit|please don.t follow up)\b", text):
        return Classification(category="NOT_INTERESTED", confidence=0.99, reason="Explicit decline")
    if re.search(r"\b(out of (the )?office|on vacation|automatic reply)\b", text):
        return Classification(category="OUT_OF_OFFICE", confidence=0.99, reason="Automatic absence reply")
    if re.search(r"\b(i am interested|i.m interested|sounds interesting|let.s (talk|chat))\b", text):
        return Classification(category="INTERESTED", confidence=0.97, reason="Explicit interest")
    return None


def ingest(app, event):
    if not event.get("id") or len(event.get("body", "")) > 100000:
        raise ValueError("Invalid inbound event")
    sender = parseaddr(event.get("sender", ""))[1].lower()
    with app.store.tx() as db:
        contact = None
        if event.get("thread_id"):
            contact = db.execute("select * from contacts where thread_id=?", (event["thread_id"],)).fetchone()
        if not contact and event.get("in_reply_to"):
            contact = db.execute(
                """select c.* from contacts c join messages m on m.contact_id=c.id
                where m.rfc_id=? and m.status in ('SENT','SENDING','UNKNOWN')""",
                (event["in_reply_to"],),
            ).fetchone()
        if not contact:
            return False
        if sender != contact["email"] and not event.get("bounce"):
            return False
        if db.execute("select 1 from inbound where id=?", (event["id"],)).fetchone():
            return False
        text = new_text(event.get("body", ""))
        early = deterministic(text, event.get("bounce", False))
        db.execute(
            "insert into inbound(id,contact_id,thread_id,body,sender,received_at,rfc_id) values (?,?,?,?,?,?,?)",
            (
                event["id"],
                contact["id"],
                event.get("thread_id"),
                text,
                sender,
                now().isoformat(),
                event.get("rfc_id", ""),
            ),
        )
        db.execute("update contacts set reply_hold=1 where id=?", (contact["id"],))
        db.execute(
            "update followups set status='CANCELLED' where contact_id=? and status in ('PENDING','QUEUED')",
            (contact["id"],),
        )
        db.execute(
            """update messages set status='CANCELLED' where contact_id=? and status in
            ('REVIEW','APPROVED','DRY_RUN','SENDING') and category in ('initial','followup')""",
            (contact["id"],),
        )
        # Opt-out and hard bounce take effect even if the classifier/worker is down.
        if early and early.category == "UNSUBSCRIBE":
            db.execute(
                "insert or ignore into suppressions values (?,?,?,?)",
                (contact["email"], contact["company_id"], "Recipient opt-out", now().isoformat()),
            )
            app.transition(contact["id"], "SUPPRESSED", db)
        elif early and early.category == "BOUNCE":
            db.execute("update contacts set invalid=1 where id=?", (contact["id"],))
        app.store.enqueue(
            f"classify:{event['id']}",
            "classify",
            {"inbound_id": event["id"], "bounce": bool(event.get("bounce"))},
            db=db,
        )
        app.store.audit("reply.received", contact["id"], {"message_id": event["id"]}, db)
    return True


def classify(app, inbound_id, bounce=False):
    event = app.store.one("select * from inbound where id=?", (inbound_id,))
    if not event:
        raise ValueError("Unknown inbound message")
    if event["classification"]:
        return {"category": event["classification"], "confidence": event["confidence"]}
    contact = app.store.one("select * from contacts where id=?", (event["contact_id"],))
    campaign = app.campaign(contact["campaign_id"])
    bounce = bounce or bool(contact["invalid"])
    result = deterministic(event["body"], bounce)
    if not result:
        result = Classification.model_validate(
            app.model.complete(
                "classify",
                {
                    "message": event["body"],
                    "untrusted_data": True,
                    "categories": sorted(CATEGORIES),
                    "context": {"last_state": contact["state"]},
                },
            )
        )
    if result.category not in CATEGORIES or result.confidence < campaign.confidence_threshold:
        result = Classification(
            category="UNCERTAIN", confidence=result.confidence, reason="Low confidence or unknown category"
        )
    with app.store.tx() as db:
        current = db.execute("select * from inbound where id=?", (inbound_id,)).fetchone()
        if current["classification"]:
            return {"category": current["classification"], "confidence": current["confidence"]}
        db.execute(
            "update inbound set classification=?,confidence=? where id=?",
            (result.category, result.confidence, inbound_id),
        )
        state = db.execute("select state from contacts where id=?", (contact["id"],)).fetchone()[0]
        if state == "SUPPRESSED":
            return result.model_dump()
        target = {
            "INTERESTED": "ENGAGED",
            "NOT_INTERESTED": "LOST",
            "UNSUBSCRIBE": "SUPPRESSED",
            "BOUNCE": "BOUNCED",
        }.get(result.category, "REVIEW")
        # Late replies never reopen won/lost or progressed opportunities without operator review.
        from .service import TRANSITIONS

        if target in TRANSITIONS.get(state, set()) or target == "SUPPRESSED":
            app.transition(contact["id"], target, db)
        if result.category == "UNSUBSCRIBE":
            db.execute(
                "insert or ignore into suppressions values (?,?,?,?)",
                (contact["email"], contact["company_id"], "Recipient opt-out", now().isoformat()),
            )
        if result.category == "BOUNCE":
            db.execute("update contacts set invalid=1 where id=?", (contact["id"],))
        if result.category == "INTERESTED":
            db.execute(
                "insert or ignore into opportunities(contact_id,status,created_at) values (?,'OPEN',?)",
                (contact["id"], now().isoformat()),
            )
        if result.category == "NOT_INTERESTED":
            db.execute("update opportunities set status='LOST' where contact_id=?", (contact["id"],))
        if result.category in {"INTERESTED", "QUESTION", "SCHEDULING", "UNCERTAIN", "OUT_OF_OFFICE"}:
            app.store.escalate(contact["id"], f"{result.category}: {result.reason}", db)
        app.store.audit("reply.classified", contact["id"], result.model_dump(), db)
    return result.model_dump()
