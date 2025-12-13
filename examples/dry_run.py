"""Offline executable scenario: real business logic, fixture model/mail/calendar APIs.

No network, credentials, mailbox reads or external messages. Does not claim fixture prose
was edited by a live LLM; verifies the actual installed skill is supplied to that stage.
"""

import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import tempfile
from pipeline.db import Store
from pipeline.models import Campaign, Seed, Evidence, Mode, now
from pipeline.service import Pipeline
from pipeline.crm import CRM, digest_report
from pipeline.calendar import Scheduler


class FixtureModel:
    def __init__(self):
        self.edit_count = 0

    def complete(self, task, payload):
        if task == "qualify":
            return {
                "score": 0.93,
                "confidence": 0.96,
                "reason": "Published intake-document workflow fits assessment offer",
            }
        if task == "write":
            return {
                "subject": "A question about document intake",
                "body": "Hi Sam,\n\nYour site describes reviewing client intake documents. Could a short conversation help identify whether one part of that review is worth automating?",
            }
        if task == "edit":
            assert "name: no-ai-slop" in payload["skill"] and "Final read" in payload["evaluation"]
            self.edit_count += 1
            return {
                "body": payload["draft"],
                "changes": "Fixture preserves specific detail and direct request.",
                "checks_passed": True,
            }
        if task == "ground":
            return {"supported": True, "confidence": 0.98, "unsupported_claims": []}
        if task == "classify":
            return {"category": "UNCERTAIN", "confidence": 0.4, "reason": "Fixture escalates ambiguous text"}
        if task == "proposal":
            return {
                "scope": "Assess one intake-document review step",
                "hours": 2,
                "assumptions": ["Customer provides non-sensitive examples"],
                "deliverables": ["Findings and a proposed next step"],
                "exclusions": ["Production deployment"],
            }
        raise ValueError(task)


class FixtureMail:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message["id"])
        return {"id": "fixture-" + message["id"], "threadId": "fixture-thread"}

    def find_sent(self, rfc_id):
        return None


class FixtureCalendar:
    def __init__(self):
        self.events = {}

    def get_event(self, calendar, id):
        return self.events.get(id)

    def freebusy(self, calendar, start, end):
        return []

    def insert(self, calendar, body):
        self.events[body["id"]] = body
        return body


def run(state):
    model, mail = FixtureModel(), FixtureMail()
    app = Pipeline(
        Store(Path(state) / "pipeline.db"),
        model,
        mail,
        os.environ.get("NO_AI_SLOP_DIR", str(Path.home() / ".agents/skills/no-ai-slop")),
    )
    campaign = Campaign(
        id="fixture",
        name="Offline fixture",
        mode=Mode.DRY_RUN,
        keywords=["documents"],
        offer="A free two-hour assessment of one intake-document workflow.",
        offer_hours=2,
        sender_name="Demo sender",
        sender_email="sender@example.com",
        postal_address="123 Example St, Example City, CA 90001",
    )
    app.configure(campaign)
    seed = Seed(
        company="Fixture Agency",
        domain="example.org",
        email="sam@example.org",
        name="Sam",
        role="Owner",
        region="US",
        region_reviewed=True,
        legal_basis="Fixture only, no live recipient",
        provenance="Synthetic fixture",
        source_url="https://example.org/contact",
        verification="published",
        evidence=[
            Evidence(
                url="https://example.org/services",
                quote="We review client intake documents.",
                text="We review client intake documents. Sam is our owner.",
            )
        ],
    )
    contact = app.prepare("fixture", seed)
    draft = app.store.one("select * from messages")
    assert draft["status"] == "DRY_RUN" and not mail.messages
    # Switch to REVIEW in this isolated fixture database solely to exercise approved mock delivery.
    campaign.mode = Mode.REVIEW
    app.configure(campaign)
    app.store.execute("delete from messages")
    content = app.writer.compose(campaign, seed)
    message_id = app.queue_message(contact["id"], content, "initial")
    app.approve(message_id)
    assert app.send(message_id) == "SENT"
    assert app.send(message_id) == "SENT" and len(mail.messages) == 1
    follow = app.store.one("select * from followups")
    assert follow["status"] == "PENDING"
    incoming = {
        "id": "fixture-reply",
        "thread_id": "fixture-thread",
        "sender": seed.email,
        "rfc_id": "<fixture-reply@example.org>",
        "body": "I am interested. Let us talk about this.",
    }
    assert app.ingest(incoming) and not app.ingest(incoming)
    assert app.store.one("select status from followups")["status"] == "CANCELLED"
    app.classify(incoming["id"])
    calendar = FixtureCalendar()
    start = (now() + timedelta(days=3)).replace(hour=18, minute=0, second=0, microsecond=0)
    while start.weekday() >= 5:
        start += timedelta(days=1)
    scheduler = Scheduler(app, calendar)
    scheduler.book(contact["id"], start, approved=True)
    scheduler.book(contact["id"], start, approved=True)
    assert len(calendar.events) == 1
    app.transition(contact["id"], "MEETING_COMPLETED")
    crm = CRM(app)
    crm.proposal(contact["id"])
    project = crm.win(contact["id"], free=True, approved=True)
    crm.complete(
        project["id"], hours=2, revenue=0, verified_results="Fixture result only; no real customer claim."
    )
    summary = digest_report(app)
    return {
        "scenario": "offline end-to-end dry run, all providers are fixtures",
        "external_emails_sent": 0,
        "mock_emails_sent": len(mail.messages),
        "editor_invocations_with_actual_skill": model.edit_count,
        "duplicate_reply_ignored": True,
        "pending_followup_cancelled": True,
        "mock_calendar_events": len(calendar.events),
        "project_state": "PROJECT_COMPLETED",
        "digest": summary,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", help="Save JSON verification report")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="pipeline-dry-run-") as state:
        result = run(state)
    rendered = json.dumps(result, indent=2, default=str)
    if args.output:
        Path(args.output).write_text(rendered + "\n")
    print(rendered)
