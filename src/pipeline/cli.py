"""Small operator interface. Run `pipeline --help` for supported commands."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from .models import Campaign, Seed, now
from .runtime import build
from .settings import SettingsStore
from .crm import CRM, metrics, digest_report, optimize


def output(value):
    print(json.dumps(value, indent=2, default=str))


def parser():
    p = argparse.ArgumentParser(prog="pipeline")
    p.add_argument("--state", default=os.environ.get("PIPELINE_STATE", "state"))
    sub = p.add_subparsers(dest="command", required=True)
    web = sub.add_parser("serve", help="Open the local setup and operator app")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--open", action="store_true", dest="open_browser")
    init = sub.add_parser("init", help="Create state and load campaign JSON files")
    init.add_argument("--config-dir", default="config")
    configure = sub.add_parser("configure")
    configure.add_argument("file")
    for name in [
        "status",
        "queue",
        "conversations",
        "followups",
        "meetings",
        "escalations",
        "failures",
        "digest",
        "optimize",
        "poll",
        "tick",
        "doctor",
    ]:
        sub.add_parser(name)
    for name in ["pause", "resume", "metrics"]:
        cmd = sub.add_parser(name)
        cmd.add_argument("campaign", nargs="?")
    auth = sub.add_parser("auth")
    auth.add_argument("--client-file", required=True)
    authorize = sub.add_parser(
        "authorize", help="Authorize exactly this AUTONOMOUS campaign hash for live sends"
    )
    authorize.add_argument("campaign")
    authorize.add_argument("--hash", required=True)
    prepare = sub.add_parser("prepare", help="Import a sourced prospect and prepare outreach")
    prepare.add_argument("campaign")
    prepare.add_argument("file")
    for name in ["discover", "schedule-discovery"]:
        cmd = sub.add_parser(name)
        cmd.add_argument("campaign")
    research = sub.add_parser("research")
    research.add_argument("url")
    for name in ["inspect", "takeover", "proposal", "meeting-completed", "redraft"]:
        cmd = sub.add_parser(name)
        cmd.add_argument("contact", type=int)
    review = sub.add_parser("contact-review")
    review.add_argument("contact", type=int)
    review.add_argument("--region", choices=["US"], required=True)
    review.add_argument("--legal-basis", required=True)
    review.add_argument("--provenance", required=True)
    approve = sub.add_parser("approve", help="Approve listed message IDs and enqueue sending")
    approve.add_argument("ids", nargs="+")
    for name in ["send", "reconcile", "replay", "cancel"]:
        cmd = sub.add_parser(name)
        cmd.add_argument("id")
    suppress = sub.add_parser("suppress")
    suppress.add_argument("email")
    suppress.add_argument("--reason", default="Operator opt-out")
    event = sub.add_parser("ingest", help="Ingest a provider event file; does not send")
    event.add_argument("file")
    classify = sub.add_parser("classify")
    classify.add_argument("id")
    slots = sub.add_parser("slots")
    slots.add_argument("campaign")
    slots.add_argument("--start", required=True, help="ISO 8601 with explicit timezone")
    book = sub.add_parser("book")
    book.add_argument("contact", type=int)
    book.add_argument("--start", required=True)
    book.add_argument("--approve-invitation", action="store_true")
    respond = sub.add_parser("respond", help="Draft a grounded response; commitments always remain in review")
    respond.add_argument("contact", type=int)
    respond.add_argument(
        "--category",
        choices=["factual_answer", "scheduling", "proposal", "testimonial", "referral"],
        default="factual_answer",
    )
    win = sub.add_parser("win")
    win.add_argument("contact", type=int)
    win.add_argument("--free", action="store_true")
    win.add_argument("--approve-scope", action="store_true")
    complete = sub.add_parser("complete")
    complete.add_argument("project", type=int)
    complete.add_argument("--hours", type=float, required=True)
    complete.add_argument("--revenue", type=float, required=True)
    complete.add_argument("--cost", type=float, default=0)
    complete.add_argument("--customer-value", default="")
    complete.add_argument("--verified-results", default="")
    case = sub.add_parser("case-study")
    case.add_argument("project", type=int)
    outcome = sub.add_parser("record-feedback")
    outcome.add_argument("project", type=int)
    outcome.add_argument("--testimonial")
    outcome.add_argument("--referral")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    app = build(args.state)
    store = app.store
    cmd = args.command
    try:
        if cmd == "serve":
            from .web import serve

            serve(args.state, args.port, args.open_browser)
        elif cmd == "init":
            files = sorted(Path(args.config_dir).glob("*.json"))
            if not files:
                raise ValueError("No campaign JSON files found")
            for file in files:
                app.configure(Campaign.model_validate_json(file.read_text()))
            output(
                {
                    "initialized": str(store.path),
                    "campaigns": [r["id"] for r in store.all("select id from campaigns")],
                }
            )
        elif cmd == "configure":
            campaign = Campaign.model_validate_json(Path(args.file).read_text())
            app.configure(campaign)
            output(store.one("select id,config_hash,approved_hash from campaigns where id=?", (campaign.id,)))
        elif cmd == "status":
            output(
                {
                    "global_paused": store.one("select value from settings where key='paused'")["value"]
                    == "true",
                    "campaigns": store.all(
                        "select id,config,config_hash,approved_hash,paused from campaigns"
                    ),
                }
            )
        elif cmd in {"pause", "resume"}:
            with store.tx() as db:
                if args.campaign:
                    if not db.execute("select id from campaigns where id=?", (args.campaign,)).fetchone():
                        raise ValueError("Unknown campaign")
                    db.execute(
                        "update campaigns set paused=? where id=?", (int(cmd == "pause"), args.campaign)
                    )
                else:
                    db.execute(
                        "update settings set value=? where key='paused'",
                        ("true" if cmd == "pause" else "false",),
                    )
                store.audit("operator." + cmd, args.campaign or "global", db=db)
            output({"paused": cmd == "pause", "campaign": args.campaign or "ALL"})
        elif cmd == "authorize":
            with store.tx() as db:
                row = db.execute("select * from campaigns where id=?", (args.campaign,)).fetchone()
                if not row or row["config_hash"] != args.hash:
                    raise ValueError("Exact current configuration hash is required")
                campaign = Campaign.model_validate_json(row["config"])
                if campaign.mode != "AUTONOMOUS":
                    raise ValueError("Set campaign mode to AUTONOMOUS in JSON and configure it first")
                if not all([campaign.sender_email, campaign.sender_name]):
                    raise ValueError("Real sender name and email required")
                db.execute("update campaigns set approved_hash=config_hash where id=?", (args.campaign,))
                store.audit("campaign.authorized", args.campaign, {"hash": args.hash}, db)
            output({"authorized": args.campaign, "hash": args.hash})
        elif cmd == "auth":
            from .auth import authorize

            authorize(
                args.client_file,
                os.environ.get("PIPELINE_GOOGLE_TOKEN", str(Path(args.state) / "google-token.json")),
            )
            output({"authorized": True})
        elif cmd == "prepare":
            output(app.prepare(args.campaign, Seed.model_validate_json(Path(args.file).read_text())))
        elif cmd == "research":
            from .research import LeadResearcher, PublicWeb

            output(LeadResearcher(PublicWeb(store), app.model).research(args.url).model_dump(mode="json"))
        elif cmd == "discover":
            from .research import discover

            output(discover(app, args.campaign))
        elif cmd == "schedule-discovery":
            app.campaign(args.campaign)
            store.enqueue(
                "discover:" + args.campaign + ":" + now().strftime("%Y%m%d"),
                "discover",
                {"campaign_id": args.campaign},
            )
            output({"scheduled": args.campaign})
        elif cmd == "inspect":
            contact = store.one("select * from contacts where id=?", (args.contact,))
            if not contact:
                raise ValueError("Unknown contact")
            output(
                {
                    "contact": contact,
                    "messages": store.all("select * from messages where contact_id=?", (args.contact,)),
                    "replies": store.all("select * from inbound where contact_id=?", (args.contact,)),
                    "history": store.all("select * from executions where contact_id=?", (args.contact,)),
                }
            )
        elif cmd == "redraft":
            output({"queued": app.redraft(args.contact)})
        elif cmd == "contact-review":
            with store.tx() as db:
                contact = db.execute("select * from contacts where id=?", (args.contact,)).fetchone()
                if not contact:
                    raise ValueError("Unknown contact")
                seed = Seed.model_validate_json(contact["seed"])
                seed.region = args.region
                seed.region_reviewed = True
                seed.legal_basis = args.legal_basis
                seed.provenance = args.provenance
                db.execute("update contacts set seed=? where id=?", (seed.model_dump_json(), args.contact))
                store.audit("contact.reviewed", args.contact, {"region": args.region}, db)
            output({"reviewed": args.contact})
        elif cmd == "takeover":
            with store.tx() as db:
                db.execute("update contacts set takeover=1,reply_hold=1 where id=?", (args.contact,))
                db.execute("update followups set status='CANCELLED' where contact_id=?", (args.contact,))
                db.execute(
                    "update messages set status='CANCELLED' where contact_id=? and status in ('REVIEW','APPROVED')",
                    (args.contact,),
                )
                store.audit("conversation.takeover", args.contact, db=db)
            output({"manual_takeover": args.contact})
        elif cmd == "approve":
            for key in args.ids:
                app.approve(key)
                store.enqueue("send:" + key, "send", {"message_id": key})
            output({"approved": args.ids})
        elif cmd == "send":
            output({"status": app.send(args.id)})
        elif cmd == "reconcile":
            from .outbound import reconcile

            output({"status": reconcile(app, args.id)})
        elif cmd == "suppress":
            from .models import email_address

            email = email_address(args.email)
            with store.tx() as db:
                contact = db.execute("select * from contacts where email=?", (email,)).fetchone()
                db.execute(
                    "insert or ignore into suppressions values (?,?,?,?)",
                    (email, contact["company_id"] if contact else None, args.reason, now().isoformat()),
                )
                if contact:
                    app.transition(contact["id"], "SUPPRESSED", db)
                    db.execute("update followups set status='CANCELLED' where contact_id=?", (contact["id"],))
                store.audit("contact.suppressed", email, db=db)
            output({"suppressed": email})
        elif cmd == "ingest":
            output({"ingested": app.ingest(json.loads(Path(args.file).read_text()))})
        elif cmd == "classify":
            output(app.classify(args.id))
        elif cmd in {"poll", "tick", "replay", "cancel"}:
            from .worker import Runner
            from .gmail import GmailSync

            runner = Runner(app, GmailSync(app, app.mail))
            if cmd == "poll":
                runner.sync.poll()
                output({"poll": "complete"})
            elif cmd == "tick":
                for job in runner.tick():
                    runner.run(job["id"])
                output({"tick": "complete"})
            else:
                getattr(runner, cmd)(args.id)
                output({cmd: args.id})
        elif cmd in {"slots", "book"}:
            from .calendar import Scheduler, GoogleCalendar
            from .auth import google_client

            scheduler = Scheduler(app, GoogleCalendar(google_client(app.mail.token_file)))
            start = datetime.fromisoformat(args.start)
            output(
                scheduler.slots(args.campaign, start)
                if cmd == "slots"
                else scheduler.book(args.contact, start, args.approve_invitation)
            )
        elif cmd == "respond":
            contact = store.one("select * from contacts where id=?", (args.contact,))
            if not contact or contact["invalid"] or contact["state"] in {"SUPPRESSED", "LOST", "BOUNCED"}:
                raise ValueError("Unknown, closed or invalid contact")
            previous = store.one(
                "select * from messages where contact_id=? and status='SENT' order by sent_at desc limit 1",
                (args.contact,),
            )
            latest = store.one(
                "select * from inbound where contact_id=? order by received_at desc limit 1", (args.contact,)
            )
            context = {
                "thread_subject": previous["subject"] if previous else None,
                "conversation": store.all("select body from inbound where contact_id=?", (args.contact,)),
            }
            if args.category == "scheduling":
                from .calendar import Scheduler, GoogleCalendar
                from .auth import google_client

                context["fresh_slots"] = Scheduler(
                    app, GoogleCalendar(google_client(app.mail.token_file))
                ).slots(contact["campaign_id"], now())
            content = app.writer.compose(
                app.campaign(contact["campaign_id"]),
                Seed.model_validate_json(contact["seed"]),
                args.category,
                context,
            )
            key = app.queue_message(
                args.contact,
                content,
                args.category,
                reply_to=latest["rfc_id"]
                if latest and latest["rfc_id"]
                else (previous["rfc_id"] if previous else None),
            )
            output({"queued": key, "approval_required": True})
        elif cmd == "proposal":
            output(CRM(app).proposal(args.contact))
        elif cmd == "win":
            output(CRM(app).win(args.contact, args.free, args.approve_scope))
        elif cmd == "complete":
            CRM(app).complete(
                args.project, args.hours, args.revenue, args.cost, args.customer_value, args.verified_results
            )
            output({"completed": args.project})
        elif cmd == "meeting-completed":
            with store.tx() as db:
                app.transition(args.contact, "MEETING_COMPLETED", db)
                db.execute("update meetings set status='COMPLETED' where contact_id=?", (args.contact,))
            output({"completed_meeting": args.contact})
        elif cmd == "case-study":
            output(CRM(app).case_study(args.project))
        elif cmd == "record-feedback":
            with store.tx() as db:
                if not db.execute("select id from projects where id=?", (args.project,)).fetchone():
                    raise ValueError("Unknown project")
                db.execute(
                    "update projects set testimonial=coalesce(?,testimonial),referral=coalesce(?,referral) where id=?",
                    (args.testimonial, args.referral, args.project),
                )
                store.audit("project.feedback", args.project, db=db)
            output({"updated": args.project})
        elif cmd == "metrics":
            output(
                metrics(app, args.campaign)
                if args.campaign
                else [metrics(app, r["id"]) for r in store.all("select id from campaigns")]
            )
        elif cmd == "digest":
            output(digest_report(app))
        elif cmd == "optimize":
            output(optimize(app))
        elif cmd == "doctor":
            output(
                {
                    "state": str(store.path),
                    "google_oauth_present": Path(app.mail.token_file).exists(),
                    "no_ai_slop_present": (app.writer.skill_dir / "SKILL.md").exists()
                    and (app.writer.skill_dir / "eval.md").exists(),
                    "llm_key_present": bool(app.model.api_key),
                    "search_key_present": bool(
                        os.environ.get("BRAVE_SEARCH_API_KEY")
                        or SettingsStore(app.store.path.parent).load().brave_api_key
                    ),
                    "models_configured": [task for task, model in app.model.models.items() if model],
                }
            )
        else:
            queries = {
                "queue": "select id,contact_id,campaign_id,category,subject,body,status,due_at from messages where status in ('REVIEW','APPROVED','DRY_RUN','UNKNOWN','SENDING')",
                "conversations": "select c.id,c.email,c.state,i.body,i.classification from contacts c left join inbound i on i.contact_id=c.id where c.thread_id is not null",
                "followups": "select * from followups",
                "meetings": "select * from meetings",
                "escalations": "select * from escalations where status='OPEN'",
                "failures": "select * from jobs where status='DEAD' or error is not null",
            }
            output(store.all(queries[cmd]))
    except Exception as exc:
        # Do not print provider request bodies or OAuth token contents.
        if isinstance(exc, (ValueError, RuntimeError, FileNotFoundError)):
            print(f"Error: {exc}", file=sys.stderr)
        else:
            print(
                f"Error: {type(exc).__name__}; inspect workflow audit and provider configuration.",
                file=sys.stderr,
            )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
