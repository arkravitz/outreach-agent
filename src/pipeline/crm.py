"""Business outcomes, bounded proposal drafts and campaign-specific funnel reporting."""

from datetime import timedelta
import json
from pydantic import BaseModel, Field
from .models import digest, now


class Proposal(BaseModel):
    scope: str
    assumptions: list[str]
    deliverables: list[str]
    exclusions: list[str]
    hours: float = Field(gt=0, le=160)


class CRM:
    def __init__(self, app):
        self.app, self.store = app, app.store

    def proposal(self, contact_id):
        contact = self.store.one("select * from contacts where id=?", (contact_id,))
        if not contact or contact["state"] not in {"ENGAGED", "MEETING_COMPLETED", "PROPOSAL_SENT"}:
            raise ValueError("Proposal needs an engaged prospect or completed meeting")
        campaign = self.app.campaign(contact["campaign_id"])
        data = Proposal.model_validate(
            self.app.model.complete(
                "proposal",
                {
                    "campaign": campaign.model_dump(mode="json"),
                    "research": json.loads(contact["seed"]),
                    "conversation": self.store.all(
                        "select body,classification from inbound where contact_id=?", (contact_id,)
                    ),
                    "untrusted_data": True,
                },
            )
        ).model_dump()
        if data["hours"] > campaign.offer_hours:
            raise ValueError(
                "Draft exceeds the configured pilot time bound; operator must define a paid scope"
            )
        data["approval_required"] = True
        with self.store.tx() as db:
            db.execute(
                "insert into opportunities(contact_id,status,proposal,created_at) values (?,'DRAFT',?,?) on conflict(contact_id) do update set proposal=excluded.proposal,status='DRAFT',proposal_approved_hash=NULL",
                (contact_id, json.dumps(data), now().isoformat()),
            )
            self.store.escalate(
                contact_id, "Proposal scope and pricing require approval; draft has not been sent", db
            )
        return data

    def win(self, contact_id, free=False, approved=False):
        if not approved:
            raise ValueError("Explicit approval of the proposal/work commitment is required")
        contact = self.store.one("select * from contacts where id=?", (contact_id,))
        campaign = self.app.campaign(contact["campaign_id"])
        with self.store.tx() as db:
            opportunity = db.execute(
                "select * from opportunities where contact_id=?", (contact_id,)
            ).fetchone()
            if not opportunity or not opportunity["proposal"]:
                raise ValueError("Generate and inspect the scoped proposal first")
            existing = db.execute(
                "select * from projects where opportunity_id=?", (opportunity["id"],)
            ).fetchone()
            if existing:
                return dict(existing)
            count = db.execute("select count(*) from projects where free=1").fetchone()[0]
            if free and count >= campaign.max_free_projects:
                raise ValueError("Configured free project limit reached")
            state = contact["state"]
            if state == "ENGAGED":
                self.app.transition(contact_id, "PROPOSAL_SENT", db)
            elif state == "MEETING_COMPLETED":
                self.app.transition(contact_id, "PROPOSAL_SENT", db)
            self.app.transition(contact_id, "WON", db)
            db.execute(
                "update opportunities set status='WON',proposal_approved_hash=? where id=?",
                (digest(opportunity["proposal"]), opportunity["id"]),
            )
            project_id = db.execute(
                "insert into projects(opportunity_id,status,free) values (?,'ACTIVE',?)",
                (opportunity["id"], int(free)),
            ).lastrowid
            self.store.audit(
                "project.won",
                project_id,
                {"free": free, "approved_proposal": digest(opportunity["proposal"])},
                db,
            )
        return self.store.one("select * from projects where id=?", (project_id,))

    def complete(self, project_id, hours, revenue, cost=0, customer_value="", verified_results=""):
        if min(hours, revenue, cost) < 0:
            raise ValueError("Hours, revenue and costs cannot be negative")
        with self.store.tx() as db:
            project = db.execute(
                "select p.*,o.contact_id from projects p join opportunities o on o.id=p.opportunity_id where p.id=?",
                (project_id,),
            ).fetchone()
            if not project:
                raise ValueError("Unknown project")
            db.execute(
                "update projects set status='COMPLETED',hours=?,revenue=?,cost=?,customer_value=?,verified_results=?,completed_at=? where id=?",
                (hours, revenue, cost, customer_value, verified_results, now().isoformat(), project_id),
            )
            self.app.transition(project["contact_id"], "PROJECT_COMPLETED", db)
            self.store.escalate(
                project["contact_id"],
                "Project completed: confirm results, then consider testimonial and referral requests",
                db,
            )
            self.store.audit("project.completed", project_id, {"revenue": revenue, "hours": hours}, db)

    def case_study(self, project_id):
        project = self.store.one("select * from projects where id=?", (project_id,))
        if not project or project["status"] != "COMPLETED" or not project["verified_results"]:
            raise ValueError("Case study requires a completed project and operator-verified results")
        return {
            "project_id": project_id,
            "hours": project["hours"],
            "verified_results": project["verified_results"],
            "customer_reported_value": project["customer_value"],
            "testimonial": project["testimonial"],
            "publication_approval_required": True,
        }


def metrics(app, campaign_id):
    store = app.store
    contacts = store.all("select * from contacts where campaign_id=?", (campaign_id,))
    messages = store.all("select * from messages where campaign_id=? and status='SENT'", (campaign_id,))
    replies = store.all(
        "select i.* from inbound i join contacts c on c.id=i.contact_id where c.campaign_id=?", (campaign_id,)
    )
    meetings = store.all(
        "select m.* from meetings m join contacts c on c.id=m.contact_id where c.campaign_id=?",
        (campaign_id,),
    )
    opportunities = store.all(
        "select o.* from opportunities o join contacts c on c.id=o.contact_id where c.campaign_id=?",
        (campaign_id,),
    )
    projects = store.all(
        "select p.* from projects p join opportunities o on o.id=p.opportunity_id join contacts c on c.id=o.contact_id where c.campaign_id=?",
        (campaign_id,),
    )
    qualified = sum(
        c["score"] is not None
        and c["score"] >= app.campaign(campaign_id).qualification_threshold
        and c["state"] != "REJECTED"
        for c in contacts
    )
    contacted = len({m["contact_id"] for m in messages})
    positives = len({r["contact_id"] for r in replies if r["classification"] == "INTERESTED"})
    bounced = len({r["contact_id"] for r in replies if r["classification"] == "BOUNCE"})
    opted_out = len({r["contact_id"] for r in replies if r["classification"] == "UNSUBSCRIBE"})
    revenue = sum(p["revenue"] for p in projects)
    return {
        "campaign_id": campaign_id,
        "discovered": len(contacts),
        "qualified": qualified,
        "contacted": contacted,
        "replies": len({r["contact_id"] for r in replies}),
        "positive_replies": positives,
        "meetings_booked": len(meetings),
        "meetings_completed": sum(m["status"] == "COMPLETED" for m in meetings),
        "opportunities": len(opportunities),
        "proposals_drafted": sum(bool(o["proposal"]) for o in opportunities),
        "proposals_sent": len({m["contact_id"] for m in messages if m["category"] == "proposal"}),
        "projects_won": len(projects),
        "revenue": revenue,
        "revenue_per_qualified_prospect": revenue / qualified if qualified else 0,
        "recorded_acquisition_cost": sum(p["cost"] for p in projects),
        "customer_acquisition_cost": sum(p["cost"] for p in projects) / len(projects) if projects else None,
        "time_invested_hours": sum(p["hours"] for p in projects),
        "bounce_rate": bounced / contacted if contacted else 0,
        "unsubscribe_rate": opted_out / contacted if contacted else 0,
    }


def optimize(app):
    suggestions = []
    for row in app.store.all("select id from campaigns"):
        m = metrics(app, row["id"])
        if m["bounce_rate"] > 0.03 or m["unsubscribe_rate"] > 0.01:
            action = "Pause acquisition and review contact provenance and relevance."
        elif m["projects_won"] >= 2 and m["revenue"] == 0:
            action = "Review verified project results and consider a bounded paid offer."
        elif m["positive_replies"]:
            action = "Prioritize interested prospects and clarify the next customer conversation."
        else:
            action = (
                "Review a small set of high-fit prospects; test one wording or targeting change at a time."
            )
        suggestions.append(
            {"campaign_id": row["id"], "suggestion": action, "configuration_changes_applied": False}
        )
    return suggestions


def digest_report(app):
    since = (now() - timedelta(days=1)).isoformat()
    escalations = app.store.all("select * from escalations where status='OPEN'")
    return {
        "generated_at": now().isoformat(),
        "new_prospects_contacted": app.store.one(
            "select count(distinct contact_id) n from messages where status='SENT' and category='initial' and sent_at>=?",
            (since,),
        )["n"],
        "positive_replies": app.store.one(
            "select count(*) n from inbound where classification='INTERESTED' and received_at>=?", (since,)
        )["n"],
        "meetings_booked": app.store.all("select * from meetings where status='BOOKED'"),
        "opportunities_requiring_attention": escalations,
        "workflow_failures": app.store.all("select id,kind,status,error from jobs where status='DEAD'"),
        "campaign_performance": [metrics(app, r["id"]) for r in app.store.all("select id from campaigns")],
        "suggestions": optimize(app),
        "highest_leverage_action": "Respond to interested prospects and resolve open escalations."
        if escalations
        else "Review and approve the best grounded outreach drafts.",
    }
