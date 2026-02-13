"""Finite preparation DAG; reply conversations live in a separate state machine."""

from datetime import datetime, timedelta
import json
import uuid
from .db import Store
from .models import Campaign, Seed, Qualification, digest, now
from .writing import Writer
from .prompt_context import prospect_campaign, feedback_context

# The graph is finite and acyclic. Persisted node outputs resume after process restarts.
PREPARATION_DAG = {
    "research": (),
    "qualify": ("research",),
    "draft": ("qualify",),
}

TRANSITIONS = {
    "DISCOVERED": {"RESEARCHING"},
    "RESEARCHING": {"QUALIFIED", "REJECTED", "NEEDS_RESEARCH"},
    "NEEDS_RESEARCH": {"RESEARCHING", "QUALIFIED"},
    "QUALIFIED": {"READY_FOR_OUTREACH"},
    "READY_FOR_OUTREACH": {"AWAITING_REPLY"},
    "AWAITING_REPLY": {"ENGAGED", "LOST", "SUPPRESSED", "BOUNCED", "REVIEW"},
    "ENGAGED": {"MEETING_BOOKED", "PROPOSAL_SENT", "LOST", "REVIEW"},
    "REVIEW": {"ENGAGED", "LOST", "MEETING_BOOKED"},
    "MEETING_BOOKED": {"MEETING_COMPLETED", "LOST"},
    "MEETING_COMPLETED": {"PROPOSAL_SENT", "LOST"},
    "PROPOSAL_SENT": {"WON", "LOST"},
    "WON": {"PROJECT_COMPLETED"},
    "PROJECT_COMPLETED": set(),
    "REJECTED": set(),
    "LOST": set(),
    "SUPPRESSED": set(),
    "BOUNCED": set(),
}


class Pipeline:
    def __init__(self, store: Store, model, mail, skill_dir, profile_excluded_terms=None):
        self.store = store
        self.model = model
        self.mail = mail
        self.writer = Writer(model, skill_dir, excluded_terms=profile_excluded_terms)
        # New policy defaults change canonical hashes. Upgrade only missing fields;
        # arbitrary hash mismatches remain fail-closed rather than being repaired.
        for row in self.store.all("select id,config from campaigns"):
            raw = json.loads(row["config"])
            canonical = Campaign.model_validate(raw).model_dump(mode="json")
            if canonical.keys() - raw.keys():
                self.store.execute(
                    "update campaigns set config=?,config_hash=?,approved_hash=NULL where id=? and config=?",
                    (json.dumps(canonical), digest(canonical), row["id"], row["config"]),
                )

    def configure(self, campaign: Campaign):
        if campaign.working_end <= campaign.working_start:
            raise ValueError("Working hours must end after they start")
        config = campaign.model_dump(mode="json")
        with self.store.tx() as db:
            db.execute(
                """insert into campaigns(id,config,config_hash,created_at) values (?,?,?,?)
                on conflict(id) do update set config=excluded.config,config_hash=excluded.config_hash""",
                (campaign.id, json.dumps(config), digest(config), now().isoformat()),
            )
            self.store.audit("campaign.configured", campaign.id, {"hash": digest(config)}, db)

    def campaign(self, campaign_id):
        row = self.store.one("select * from campaigns where id=?", (campaign_id,))
        if not row:
            raise ValueError("Unknown campaign")
        return Campaign.model_validate_json(row["config"])

    def transition(self, contact_id, target, db=None):
        def apply(conn):
            row = conn.execute("select state from contacts where id=?", (contact_id,)).fetchone()
            if not row:
                raise ValueError("Unknown contact")
            current = row["state"]
            if current == target:
                return
            if target != "SUPPRESSED" and target not in TRANSITIONS.get(current, set()):
                raise ValueError(f"Invalid transition {current} -> {target}")
            conn.execute(
                "update contacts set state=?,updated_at=? where id=?", (target, now().isoformat(), contact_id)
            )
            self.store.audit("contact.transition", contact_id, {"from": current, "to": target}, conn)

        if db is not None:
            apply(db)
        else:
            with self.store.tx() as conn:
                apply(conn)

    def step(self, contact_id, name, fn):
        key = f"prepare:{contact_id}:{name}"
        row = self.store.one("select * from executions where id=?", (key,))
        if row and row["status"] == "DONE":
            return json.loads(row["result"])
        with self.store.tx() as db:
            db.execute(
                """insert into executions(id,contact_id,step,status,updated_at) values (?,?,?,'RUNNING',?)
                on conflict(id) do update set status='RUNNING',attempts=attempts+1,updated_at=excluded.updated_at""",
                (key, contact_id, name, now().isoformat()),
            )
        try:
            result = fn()
        except Exception as exc:
            self.store.execute(
                "update executions set status='FAILED',error=?,updated_at=? where id=?",
                (type(exc).__name__, now().isoformat(), key),
            )
            raise
        self.store.execute(
            "update executions set status='DONE',result=?,error=NULL,updated_at=? where id=?",
            (json.dumps(result, default=str), now().isoformat(), key),
        )
        return result

    def prepare(self, campaign_id, seed: Seed, *, draft_requested=False):
        campaign = self.campaign(campaign_id)
        for evidence in seed.evidence:
            if not evidence.verified or evidence.quote not in evidence.text:
                raise ValueError("Evidence quote is unverified or absent from source text")
            if not evidence.url.startswith("https://"):
                raise ValueError("Evidence requires an HTTPS source")
        timestamp = now().isoformat()
        with self.store.tx() as db:
            existing = db.execute("select * from contacts where email=?", (seed.email,)).fetchone()
            if existing:
                contact_id = existing["id"]
                if existing["state"] not in {"DISCOVERED", "RESEARCHING", "QUALIFIED", "NEEDS_RESEARCH"}:
                    return dict(existing)
                # Resume the original campaign and immutable research, never switch treatments.
                campaign_id = existing["campaign_id"]
                seed = Seed.model_validate_json(existing["seed"])
                campaign = self.campaign(campaign_id)
            else:
                db.execute(
                    "insert or ignore into companies(domain,name) values (?,?)",
                    (seed.domain.lower(), seed.company),
                )
                company_id = db.execute(
                    "select id from companies where domain=?", (seed.domain.lower(),)
                ).fetchone()[0]
                other = db.execute("select * from contacts where company_id=?", (company_id,)).fetchone()
                if other:
                    return dict(other)  # One active contact per company in this MVP.
                variant = None
                if campaign.experiment_variants:
                    variant = campaign.experiment_variants[
                        int(digest(seed.domain), 16) % len(campaign.experiment_variants)
                    ]
                    db.execute(
                        "insert or ignore into experiments values (?,?,?,?)",
                        (company_id, campaign_id, variant, timestamp),
                    )
                cursor = db.execute(
                    """insert into contacts(company_id,campaign_id,email,name,domain,state,seed,
                    created_at,updated_at,variant) values (?,?,?,?,?,'DISCOVERED',?,?,?,?)""",
                    (
                        company_id,
                        campaign_id,
                        seed.email,
                        seed.name,
                        seed.domain.lower(),
                        seed.model_dump_json(),
                        timestamp,
                        timestamp,
                        variant,
                    ),
                )
                contact_id = cursor.lastrowid
                self.store.enqueue(f"prepare:{contact_id}", "prepare", {"contact_id": contact_id}, db=db)
        from .outbound import Blocked

        lease_id = f"preparelease:{contact_id}"
        owner = json.dumps({"owner": uuid.uuid4().hex})
        with self.store.tx() as db:
            lease = db.execute("select * from executions where id=?", (lease_id,)).fetchone()
            if (
                lease
                and lease["status"] == "RUNNING"
                and lease["next_at"]
                and datetime.fromisoformat(lease["next_at"]) > now()
            ):
                raise Blocked("Prospect preparation is already running; its durable task will retry safely")
            db.execute(
                """insert into executions(id,contact_id,step,status,result,next_at,updated_at)
                values (?,?,'lease','RUNNING',?,?,?) on conflict(id) do update set status='RUNNING',
                result=excluded.result,next_at=excluded.next_at,updated_at=excluded.updated_at""",
                (lease_id, contact_id, owner, (now() + timedelta(minutes=15)).isoformat(), now().isoformat()),
            )
        try:
            return self._prepare_contact(campaign, seed, contact_id, draft_requested=draft_requested)
        finally:
            self.store.execute(
                "update executions set status='DONE',next_at=NULL,updated_at=? where id=? and result=?",
                (now().isoformat(), lease_id, owner),
            )

    def _prepare_contact(self, campaign, seed, contact_id, *, draft_requested=False):
        contact = self.store.one("select * from contacts where id=?", (contact_id,))
        if contact["state"] in {"DISCOVERED", "NEEDS_RESEARCH"}:
            self.transition(contact_id, "RESEARCHING")
        self.step(
            contact_id, "research", lambda: {"evidence": [e.model_dump(mode="json") for e in seed.evidence]}
        )
        qual = self.step(
            contact_id,
            "qualify",
            lambda: Qualification.model_validate(
                self.model.complete(
                    "qualify",
                    {
                        "campaign": prospect_campaign(campaign),
                        "recipient": seed.research_context(),
                        "operator_feedback": feedback_context(
                            self.store.all(
                                "select verdict,note from prospect_feedback f join contacts c on c.id=f.contact_id where c.campaign_id=? order by f.updated_at desc limit 6",
                                (campaign.id,),
                            )
                        ),
                        "untrusted_data": True,
                    },
                )
            ).model_dump(),
        )
        self.store.execute(
            "update contacts set score=?,reason=? where id=?", (qual["score"], qual["reason"], contact_id)
        )
        request = self.store.one(
            "select value from sync_state where key=?", ("draft_request:" + str(contact_id),)
        )
        draft_requested = draft_requested or bool(
            request and request["value"] == digest(campaign.model_dump(mode="json"))
        )
        automatic = (
            campaign.auto_draft
            and qual["score"] >= campaign.auto_draft_threshold
            and self.draftable_contact(contact, campaign)
        )
        if campaign.prospecting_goal == "RELATIONSHIPS":
            draft_requested = draft_requested or automatic
            target = (
                "QUALIFIED"
                if draft_requested or (qual["score"] >= 0.35 and qual["confidence"] >= 0.5)
                else "NEEDS_RESEARCH"
            )
            self.transition(contact_id, target)
            if not draft_requested:
                return self.store.one("select * from contacts where id=?", (contact_id,))
        elif (
            qual["score"] < campaign.qualification_threshold
            or qual["confidence"] < campaign.confidence_threshold
        ):
            self.transition(contact_id, "REJECTED")
            return self.store.one("select * from contacts where id=?", (contact_id,))
        if self.store.one("select state from contacts where id=?", (contact_id,))["state"] == "RESEARCHING":
            self.transition(contact_id, "QUALIFIED")
        message = self.step(contact_id, "draft", lambda: self.writer.compose(campaign, seed))
        with self.store.tx() as db:
            if not db.execute(
                "select id from messages where contact_id=? and category='initial'", (contact_id,)
            ).fetchone():
                self.queue_message(contact_id, message, "initial", db=db)
            self.transition(contact_id, "READY_FOR_OUTREACH", db)
        return self.store.one("select * from contacts where id=?", (contact_id,))

    def draftable_contact(self, contact, campaign):
        from .research import LeadResearcher

        if (
            contact["takeover"]
            or contact["reply_hold"]
            or contact["invalid"]
            or contact["domain"] in campaign.blocked_domains
        ):
            return False
        if not LeadResearcher.suitable_email(contact["email"]):
            return False
        if self.store.one(
            "select email from suppressions where email=? or company_id=?",
            (contact["email"], contact["company_id"]),
        ):
            return False
        feedback = self.store.one(
            "select verdict from prospect_feedback where contact_id=?", (contact["id"],)
        )
        return not feedback or feedback["verdict"] == "GOOD"

    def queue_automatic_drafts(self):
        queued = 0
        for row in self.store.all("select id,config_hash,paused from campaigns"):
            campaign = self.campaign(row["id"])
            if row["paused"] or not campaign.auto_draft or campaign.prospecting_goal != "RELATIONSHIPS":
                continue
            for contact in self.store.all(
                "select * from contacts where campaign_id=? and state in ('QUALIFIED','NEEDS_RESEARCH','READY_FOR_OUTREACH') and score>=?",
                (campaign.id, campaign.auto_draft_threshold),
            ):
                if not self.draftable_contact(contact, campaign):
                    continue
                kind = "prepare"
                if contact["state"] == "READY_FOR_OUTREACH":
                    message = self.store.one(
                        "select config_hash,status from messages where contact_id=? and category='initial' and status!='CANCELLED' order by created_at desc limit 1",
                        (contact["id"],),
                    )
                    if (
                        not message
                        or message["config_hash"] == row["config_hash"]
                        or message["status"] not in {"REVIEW", "APPROVED", "DRY_RUN"}
                    ):
                        continue
                    kind = "redraft"
                job_id = f"auto-draft:{contact['id']}:{row['config_hash'][:12]}"
                if not self.store.one("select id from jobs where id=?", (job_id,)):
                    self.store.enqueue(job_id, kind, {"contact_id": contact["id"]})
                    queued += 1
        return queued

    def prepare_outreach(self, contact_id):
        contact = self.store.one("select * from contacts where id=?", (contact_id,))
        if not contact or contact["state"] not in {"QUALIFIED", "NEEDS_RESEARCH"}:
            raise ValueError("Choose a retained prospect that has not already been drafted")
        if (
            contact["takeover"]
            or contact["reply_hold"]
            or contact["invalid"]
            or self.store.one("select email from suppressions where email=?", (contact["email"],))
        ):
            raise ValueError("Contact is suppressed, unavailable or under manual control")
        campaign = self.campaign(contact["campaign_id"])
        if campaign.prospecting_goal != "RELATIONSHIPS":
            raise ValueError("Manual prospect selection is available for relationship campaigns")
        from .research import LeadResearcher

        if not LeadResearcher.suitable_email(contact["email"]):
            raise ValueError("Choose a suitable business contact; hiring and legal inboxes are excluded")
        config_hash = digest(campaign.model_dump(mode="json"))
        self.store.execute(
            "insert into sync_state values (?,?) on conflict(key) do update set value=excluded.value",
            ("draft_request:" + str(contact_id), config_hash),
        )
        self.store.enqueue(
            f"prepare-selected:{contact_id}:{config_hash[:12]}", "prepare", {"contact_id": contact_id}
        )
        self.store.audit("prospect.selected", contact_id, {"action": "prepare_review_draft"})
        result = self.prepare(
            contact["campaign_id"], Seed.model_validate_json(contact["seed"]), draft_requested=True
        )
        return result

    def queue_message(self, contact_id, content, category, due_at=None, db=None, reply_to=None):
        contact = self.store.one("select * from contacts where id=?", (contact_id,))
        campaign = self.campaign(contact["campaign_id"])
        config_hash = digest(campaign.model_dump(mode="json"))
        if content.get("campaign_hash") != config_hash:
            raise ValueError(
                "Campaign configuration changed during drafting; regenerate under current settings"
            )
        key = uuid.uuid4().hex
        rfc_id = f"<pipeline.{key}@{campaign.sender_email.split('@')[-1] or 'invalid.local'}>"
        content_hash = digest(
            {
                "body": content["body"],
                "subject": content["subject"],
                "config": config_hash,
                "category": category,
                "reply_to": reply_to,
            }
        )
        status = "DRY_RUN" if campaign.mode == "DRY_RUN" else "REVIEW"
        args = (
            key,
            contact_id,
            campaign.id,
            category,
            content["subject"],
            content["body"],
            status,
            config_hash,
            content_hash,
            content["skill_hash"],
            json.dumps(content["edit_audit"]),
            now().isoformat(),
            (due_at or now()).isoformat(),
            rfc_id,
            reply_to,
        )
        sql = """insert into messages(id,contact_id,campaign_id,category,subject,body,status,config_hash,content_hash,
            skill_hash,edit_audit,created_at,due_at,rfc_id,reply_to) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        if db is not None:
            db.execute(sql, args)
        else:
            self.store.execute(sql, args)
        return key

    def resume(self, contact_id):
        contact = self.store.one("select * from contacts where id=?", (contact_id,))
        return self.prepare(contact["campaign_id"], Seed.model_validate_json(contact["seed"]))

    def redraft(self, contact_id, *, expected_message_id=None):
        contact = self.store.one("select * from contacts where id=?", (contact_id,))
        if not contact or contact["state"] != "READY_FOR_OUTREACH":
            raise ValueError("Redraft initial outreach only before any sending")
        if self.store.one(
            "select id from messages where contact_id=? and status in ('SENT','SENDING','UNKNOWN')",
            (contact_id,),
        ):
            raise ValueError("Sent or ambiguous messages cannot be replaced; reconcile first")
        content = self.writer.compose(
            self.campaign(contact["campaign_id"]), Seed.model_validate_json(contact["seed"])
        )
        with self.store.tx() as db:
            current = db.execute("select * from contacts where id=?", (contact_id,)).fetchone()
            if expected_message_id:
                original = db.execute(
                    "select status from messages where id=? and contact_id=?",
                    (expected_message_id, contact_id),
                ).fetchone()
                if not original or original["status"] not in {"REVIEW", "APPROVED", "DRY_RUN"}:
                    raise ValueError("Draft was discarded or changed; automatic regeneration stopped")
                if (
                    current["takeover"]
                    or current["reply_hold"]
                    or current["invalid"]
                    or db.execute(
                        "select email from suppressions where email=? or company_id=?",
                        (current["email"], current["company_id"]),
                    ).fetchone()
                ):
                    raise ValueError("Contact eligibility changed during automatic regeneration")
            reserved = db.execute(
                "select id from messages where contact_id=? and status in ('SENT','SENDING','UNKNOWN')",
                (contact_id,),
            ).fetchone()
            if current["state"] != "READY_FOR_OUTREACH" or reserved:
                raise ValueError("Contact changed during drafting; inspect it before continuing")
            db.execute(
                "update messages set status='CANCELLED' where contact_id=? and status in ('REVIEW','APPROVED','DRY_RUN')",
                (contact_id,),
            )
            key = self.queue_message(contact_id, content, "initial", db=db)
            self.store.audit("message.redrafted", key, db=db)
            return key

    def approve(self, message_id, **review):
        from .outbound import approve

        return approve(self, message_id, **review)

    def send(self, message_id):
        from .outbound import send

        return send(self, message_id)

    def ingest(self, event):
        from .replies import ingest

        return ingest(self, event)

    def classify(self, inbound_id):
        from .replies import classify

        return classify(self, inbound_id)
