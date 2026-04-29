"""Explicit operator actions over the existing domain services, shared by browser API."""

from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import threading
import time
from .models import Campaign, Seed, Qualification, digest, now, email_address
from .settings import SettingsStore, is_local
from .runtime import build
from .crm import CRM, metrics, digest_report, optimize
from .auth import SCOPES, save_token
from .worker_control import WorkerControl


class WebAPI:
    def __init__(self, state):
        self.state = Path(state).resolve()
        self.settings = SettingsStore(self.state)
        self.store = build(self.state).store
        self.worker = WorkerControl(self.state, self.store)
        self.research_views = {}
        self.research_lock = threading.Lock()
        self.oauth_flows = {}
        self.oauth_lock = threading.Lock()
        self.background = {}
        self.background_lock = threading.Lock()
        if not self.store.one("select id from campaigns limit 1"):
            defaults = Path(__file__).parent / "defaults"
            for file in sorted(defaults.glob("*.json")):
                build(self.state).configure(Campaign.model_validate_json(file.read_text()))

    def app(self):
        return build(self.state)

    def model_fingerprint(self):
        app = self.app()
        return digest(
            {
                "url": app.model.base_url,
                "key": app.model.api_key,
                "models": app.model.models,
                "output": app.model.max_output_tokens,
                "thinking": app.model.reasoning_effort,
            }
        )

    def readiness(self):
        app = self.app()
        config = self.settings.load()
        test = self.store.one("select value from sync_state where key='model_test'")
        test = json.loads(test["value"]) if test else {}
        tested = test.get("fingerprint") == self.model_fingerprint()
        campaigns = [app.campaign(r["id"]) for r in self.store.all("select id from campaigns")]
        identity = bool(campaigns) and all(c.sender_name and c.sender_email for c in campaigns)
        skill = (app.writer.skill_dir / "SKILL.md").exists() and (app.writer.skill_dir / "eval.md").exists()
        token = Path(app.mail.token_file).exists()
        model_configured = bool(app.model.models.get("write")) and (
            bool(app.model.api_key) or is_local(app.model.base_url)
        )
        return {
            "model_configured": model_configured,
            "model_tested": tested,
            "model_tested_at": test.get("tested_at"),
            "identity_complete": identity,
            "postal_address_missing": any(not c.postal_address.strip() for c in campaigns),
            "google_connected": token,
            "google_client_uploaded": (self.state / "google-client.json").exists(),
            "editor_present": skill,
            "cloud_email_allowed": config.allow_cloud_email or is_local(app.model.base_url),
            "discovery_enabled": config.discovery_enabled,
            "ready_to_research": model_configured and tested and skill,
            "ready_to_send": model_configured and tested and identity and skill and token,
        }

    def usage(self):
        day = now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        usage = self.store.one(
            """select count(*) calls,coalesce(sum(input_tokens),0) input_tokens,
            coalesce(sum(output_tokens),0) output_tokens,
            coalesce(sum(case when status='FAILED' then 1 else 0 end),0) failures
            from model_usage where created_at>=?""",
            (day,),
        )
        config = self.settings.load()
        usage["limit"] = config.daily_model_limit
        usage["local"] = is_local(self.app().model.base_url)
        priced = config.input_price_per_million > 0 or config.output_price_per_million > 0
        usage["estimated_usd"] = (
            (
                (
                    usage["input_tokens"] * config.input_price_per_million
                    + usage["output_tokens"] * config.output_price_per_million
                )
                / 1_000_000
            )
            if priced
            else None
        )
        usage["by_task"] = self.store.all(
            """select task,model,count(*) calls,sum(input_tokens) input_tokens,
            sum(output_tokens) output_tokens from model_usage where created_at>=? group by task,model""",
            (day,),
        )
        return usage

    def record_send_batch(self, ids, db=None):
        if not ids:
            return
        value = json.dumps({"ids": ids, "started_at": now().isoformat()})
        sql = "insert into sync_state values ('latest_send_batch',?) on conflict(key) do update set value=excluded.value"
        if db is not None:
            db.execute(sql, (value,))
        else:
            self.store.execute(sql, (value,))

    def delivery_status(self):
        saved = self.store.one("select value from sync_state where key='latest_send_batch'")
        if not saved:
            return None
        batch = json.loads(saved["value"])
        rows = self.store.all(
            "select m.id,m.subject,m.status,m.sent_at,m.approved_hash,m.content_hash,c.email,coalesce(j.error,m.error) error,"
            "j.due_at retry_at,j.status job_status from messages m join contacts c on c.id=m.contact_id "
            "left join jobs j on j.id='send:'||m.id "
            "where m.id in (select value from json_each(?)) order by m.sent_at desc,m.created_at desc,m.id desc",
            (json.dumps(batch["ids"]),),
        )
        counts = {}
        for row in rows:
            if (
                row["status"] == "APPROVED"
                and row["approved_hash"] == row["content_hash"]
                and row["error"] == "Message requires approval"
            ):
                row["error"] = None
            if row["retry_at"] and datetime.fromisoformat(row["retry_at"]) <= now():
                row["retry_at"] = None
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        attention = [
            r
            for r in rows
            if r["status"] in {"UNKNOWN", "FAILED"}
            or (r["status"] in {"APPROVED", "REVIEW"} and r["job_status"] in {"DEAD", "CANCELLED", "DONE"})
        ]
        attention_ids = {r["id"] for r in attention}
        waiting = [r for r in rows if r["status"] in {"APPROVED", "REVIEW"} and r["id"] not in attention_ids]
        return {
            "started_at": batch["started_at"],
            "total": len(rows),
            "counts": counts,
            "attention_count": len(attention),
            "queued_count": len(waiting),
            "sent": [r for r in rows if r["status"] == "SENT"][:5],
            "waiting": sorted(waiting, key=lambda r: not bool(r["error"]))[:5],
            "attention": attention[:5],
        }

    def snapshot(self):
        app = self.app()
        campaigns = self.store.all("select * from campaigns order by created_at")
        for row in campaigns:
            row["config"] = json.loads(row["config"])
            row["authorized"] = row["approved_hash"] == row["config_hash"]
            row["metrics"] = metrics(app, row["id"])
        contacts = self.store.all("""select c.id,c.company_id,c.campaign_id,c.email,c.name,c.domain,c.state,c.score,
            c.reason,c.created_at,c.updated_at,c.reply_hold,c.takeover,c.invalid,c.variant,co.name company,c.seed,
            (select count(*) from messages sm where sm.contact_id=c.id and sm.status='SENT') sent_count
            from contacts c join companies co on co.id=c.company_id
            where c.id in (select id from contacts order by updated_at desc limit 1000)
            or c.id in (select contact_id from messages where status in ('REVIEW','APPROVED','UNKNOWN','SENDING','DRY_RUN'))
            order by c.updated_at desc""")
        for row in contacts:
            seed = json.loads(row.pop("seed"))
            row["region"] = seed["region"]
            row["region_reviewed"] = seed["region_reviewed"]
            qual = self.store.one(
                "select result from executions where contact_id=? and step='qualify' and status='DONE'",
                (row["id"],),
            )
            row["assessment_confidence"] = json.loads(qual["result"]).get("confidence") if qual else None
            row["contact_quality"] = (
                "Named business contact"
                if seed.get("name") and seed.get("role")
                else "General business inbox; check recipient"
            )
            row["feedback"] = self.store.one(
                "select verdict,note from prospect_feedback where contact_id=?", (row["id"],)
            )
            message = self.store.one(
                "select id,status,config_hash from messages where contact_id=? and category='initial' and status!='CANCELLED' order by created_at desc limit 1",
                (row["id"],),
            )
            row["draft_message_id"] = message["id"] if message else None
            current_hash = next(c["config_hash"] for c in campaigns if c["id"] == row["campaign_id"])
            row["draft_status"] = (
                ("STALE" if message["config_hash"] != current_hash else message["status"])
                if message
                else None
            )
            row["draft_eligible"] = app.draftable_contact(row, app.campaign(row["campaign_id"]))
        tasks = []
        with self.background_lock:
            active_ids = {key for key, task in self.background.items() if task["status"] == "RUNNING"}
        for task in self.store.all(
            "select * from executions where step like 'ui:%' order by updated_at desc limit 20"
        ):
            key = task["id"].removeprefix("ui:")
            result = json.loads(task["result"]) if task["result"] else None
            interrupted = task["status"] == "RUNNING" and key not in active_ids
            tasks.append(
                {
                    "id": key,
                    "name": task["step"].removeprefix("ui:"),
                    "status": "INTERRUPTED" if interrupted else task["status"],
                    "started_at": task["updated_at"],
                    "result": result,
                    "error": "The app restarted during this action. Inspect the prospect, then retry if needed."
                    if interrupted
                    else task["error"],
                }
            )
        account = self.store.one("select value from sync_state where key='google_account'")
        return {
            "campaigns": campaigns,
            "contacts": contacts,
            "prepared_count": self.store.one(
                "select count(*) n from messages where status in ('REVIEW','APPROVED')"
            )["n"],
            "prospect_pool": {
                "contacts": self.store.one(
                    "select count(*) n from contacts where state not in ('REJECTED','SUPPRESSED','LOST','BOUNCED')"
                )["n"],
                "target": sum(c["config"].get("pool_target", 120) for c in campaigns),
                "sent": self.store.one(
                    "select count(distinct contact_id) n from messages where status='SENT'"
                )["n"],
                "reviewed_good": self.store.one(
                    "select count(*) n from prospect_feedback where verdict='GOOD'"
                )["n"],
            },
            "discovery_candidates": self.store.all(
                "select * from discovery_candidates where contact_id is null order by updated_at desc limit 100"
            ),
            "discovery_query_history": self.store.all(
                "select * from discovery_queries order by id desc limit 20"
            ),
            "messages": self.store.all(
                "select * from messages where status in ('REVIEW','APPROVED','UNKNOWN','SENDING','DRY_RUN') "
                "or id in (select id from messages order by created_at desc limit 200) order by created_at desc"
            ),
            "delivery": self.delivery_status(),
            "inbound": self.store.all("select * from inbound order by received_at desc limit 200"),
            "followups": self.store.all("select * from followups order by due_at desc limit 200"),
            "meetings": self.store.all("select * from meetings order by start desc limit 200"),
            "opportunities": self.store.all("select * from opportunities order by created_at desc limit 200"),
            "projects": self.store.all("select * from projects order by id desc limit 200"),
            "escalations": self.store.all(
                "select * from escalations where status='OPEN' order by id desc limit 200"
            ),
            "jobs": self.store.all("select * from jobs order by due_at desc limit 100"),
            "audit": self.store.all("select * from audit order by id desc limit 100"),
            "paused": self.store.one("select value from settings where key='paused'")["value"] == "true",
            "settings": self.settings.public(),
            "setup": self.readiness(),
            "usage": self.usage(),
            "worker": self.worker.status(),
            "background": tasks,
            "local_model_progress": json.loads(progress["value"])
            if (progress := self.store.one("select value from sync_state where key='local_model_progress'"))
            else None,
            "google_account": json.loads(account["value"]) if account else None,
            "suggestions": optimize(app),
            "discovery_reports": [
                json.loads(row["value"])
                for row in self.store.all("select value from sync_state where key like 'discovery_report:%'")
            ],
            "generated_at": now().isoformat(),
        }

    def background_task(self, name, fn):
        with self.background_lock:
            active = [t for t in self.background.values() if t["status"] == "RUNNING"]
            if active:
                raise ValueError("Another research/task action is running. Wait for its result in Activity.")
            key = secrets.token_hex(8)
            self.background[key] = {
                "id": key,
                "name": name,
                "status": "RUNNING",
                "started_at": now().isoformat(),
            }

        self.store.execute(
            "insert into executions(id,step,status,updated_at) values (?,?,'RUNNING',?)",
            ("ui:" + key, "ui:" + name, now().isoformat()),
        )

        def work():
            try:
                result = fn()
                status, error = "DONE", None
            except Exception as exc:
                result, status = None, "FAILED"
                error = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
            with self.background_lock:
                self.background[key].update(
                    {"status": status, "result": result, "error": error, "finished_at": now().isoformat()}
                )
            self.store.execute(
                "update executions set status=?,result=?,error=?,updated_at=? where id=?",
                (
                    status,
                    json.dumps(result, default=str) if result is not None else None,
                    error,
                    now().isoformat(),
                    "ui:" + key,
                ),
            )
            self.store.audit("ui.task." + status.lower(), key, {"task": name})

        threading.Thread(target=work, daemon=True).start()
        return {"task_id": key, "status": "RUNNING"}

    def research_view(self, key):
        with self.research_lock:
            value = self.research_views.get(key)
            if not value or value[0] <= time.monotonic():
                self.research_views.pop(key, None)
                return None
            return value[1]

    def post(self, path, data, origin, session):
        app = self.app()
        if path == "/api/messages/sent":
            offset = max(0, int(data.get("offset", 0)))
            search = str(data.get("search", "")).strip().lower()
            campaign_id = str(data.get("campaign_id", ""))
            where = "m.status='SENT'"
            args = []
            if search:
                where += " and instr(lower(c.email||' '||co.name||' '||coalesce(c.name,'')||' '||m.subject||' '||m.body),?)>0"
                args.append(search)
            if campaign_id:
                where += " and m.campaign_id=?"
                args.append(campaign_id)
            source = (
                " from messages m join contacts c on c.id=m.contact_id "
                "join companies co on co.id=c.company_id join campaigns p on p.id=m.campaign_id "
                "where " + where
            )
            total = self.store.one("select count(*) n" + source, args)["n"]
            # Clamp when deletions or a changed filter make the requested page disappear.
            offset = min(offset, max(0, (total - 1) // 50 * 50))
            messages = self.store.all(
                "select m.*,c.email,c.name,co.name company,json_extract(p.config,'$.name') campaign_name"
                + source
                + " order by m.sent_at desc,m.id desc limit 50 offset ?",
                [*args, offset],
            )
            return {"messages": messages, "total": total, "offset": offset, "limit": 50}
        if path == "/api/settings":
            result = self.settings.update(data)
            self.store.audit("settings.updated", "runtime", {"fields": sorted(data)})
            return result
        if path == "/api/google-research":
            from .google_research import render_research

            result = app.model.grounded_research(data.get("query", ""))
            page = render_research(result)
            with self.research_lock:
                # Ephemeral display state only; not a reusable source cache or lead database.
                self.research_views = {
                    key: value for key, value in self.research_views.items() if value[0] > time.monotonic()
                }
                if len(self.research_views) >= 20:
                    self.research_views.pop(next(iter(self.research_views)))
                key = secrets.token_urlsafe(24)
                self.research_views[key] = (time.monotonic() + 900, page)
            return {"viewer_url": "/google-research/" + key}
        if path == "/api/models":
            return {"models": app.model.list_models()}
        if path == "/api/model/test":
            fingerprint = self.model_fingerprint()
            result = Qualification.model_validate(
                app.model.complete(
                    "qualify",
                    {
                        "campaign": {
                            "offer": "A two-hour document workflow assessment",
                            "keywords": ["documents"],
                        },
                        "recipient": {
                            "company": "Synthetic Test Company",
                            "evidence": [{"quote": "We process paper forms."}],
                        },
                        "untrusted_data": True,
                    },
                )
            ).model_dump()
            self.store.execute(
                "insert into sync_state values ('model_test',?) on conflict(key) do update set value=excluded.value",
                (json.dumps({"fingerprint": fingerprint, "tested_at": now().isoformat()}),),
            )
            return {
                "success": True,
                "message": "Connection and structured JSON response verified. No email sent.",
                "model": app.model.models["qualify"],
                "result": result,
            }
        if path == "/api/ollama/install":
            if not data.get("confirm_download"):
                raise ValueError("Confirm the model download and local disk usage first")
            from .local_models import install_model

            settings = self.settings.load()
            model = str(data.get("model", ""))
            last_update = [0.0]
            fingerprint = digest(settings.model_dump())

            def progress(value):
                if time.monotonic() - last_update[0] >= 1 or value.get("status") == "success":
                    self.store.execute(
                        "insert into sync_state values ('local_model_progress',?) on conflict(key) do update set value=excluded.value",
                        (json.dumps(value),),
                    )
                    last_update[0] = time.monotonic()

            def install():
                result = install_model(settings, model, on_progress=progress)
                if digest(self.settings.load().model_dump()) == fingerprint:
                    self.settings.update({"default_model": result["model"], "models": {}})
                    result["selected_as_default"] = True
                else:
                    result["selected_as_default"] = False
                return result

            return self.background_task("Download and prepare local model", install)
        if path == "/api/google/client":
            config = data.get("client")
            if (
                not isinstance(config, dict)
                or set(config) != {"installed"}
                or not isinstance(config["installed"], dict)
            ):
                raise ValueError(
                    "Upload the original Google Desktop app OAuth client JSON (installed section required)"
                )
            client = config["installed"]
            if (
                not client.get("client_id", "").endswith(".apps.googleusercontent.com")
                or not client.get("client_secret")
                or client.get("auth_uri")
                not in {
                    "https://accounts.google.com/o/oauth2/auth",
                    "https://accounts.google.com/o/oauth2/v2/auth",
                }
                or client.get("token_uri") != "https://oauth2.googleapis.com/token"
            ):
                raise ValueError(
                    "Use an unmodified Google Desktop OAuth client downloaded from Google Cloud Console"
                )
            save_token(self.state / "google-client.json", json.dumps(config))
            return {"saved": True}
        if path == "/api/google/start":
            from google_auth_oauthlib.flow import InstalledAppFlow

            client_file = self.state / "google-client.json"
            if not client_file.exists():
                raise ValueError("Upload your Google Desktop OAuth client JSON first")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_file),
                SCOPES,
                redirect_uri=origin + "/oauth/callback",
                autogenerate_code_verifier=True,
            )
            url, state = flow.authorization_url(access_type="offline", prompt="select_account consent")
            with self.oauth_lock:
                self.oauth_flows = {k: v for k, v in self.oauth_flows.items() if v[0] > time.monotonic()}
                self.oauth_flows[state] = (time.monotonic() + 600, flow, session)
            return {"url": url}
        if path == "/api/google/disconnect":
            Path(app.mail.token_file).unlink(missing_ok=True)
            self.store.execute("delete from sync_state where key='google_account'")
            self.store.audit("google.disconnected", "account")
            return {"disconnected": True}
        if path == "/api/pause":
            paused = data.get("paused")
            if not isinstance(paused, bool):
                raise ValueError("paused must be true or false")
            campaign_id = data.get("campaign_id")
            with self.store.tx() as db:
                if campaign_id:
                    if not db.execute("select id from campaigns where id=?", (campaign_id,)).fetchone():
                        raise ValueError("Unknown campaign")
                    db.execute("update campaigns set paused=? where id=?", (int(paused), campaign_id))
                else:
                    db.execute(
                        "update settings set value=? where key='paused'", ("true" if paused else "false",)
                    )
                self.store.audit(
                    "operator.pause" if paused else "operator.resume", campaign_id or "global", db=db
                )
            return {"paused": paused}
        if path == "/api/campaign/save":
            campaign = Campaign.model_validate(data["campaign"])
            app.configure(campaign)
            return {
                "saved": campaign.id,
                "config_hash": self.store.one("select config_hash from campaigns where id=?", (campaign.id,))[
                    "config_hash"
                ],
            }
        if path == "/api/campaign/authorize":
            if not data.get("confirm_live_sending"):
                raise ValueError("Explicit live-sending confirmation is required")
            with self.store.tx() as db:
                row = db.execute("select * from campaigns where id=?", (data["id"],)).fetchone()
                if not row or row["config_hash"] != data.get("hash"):
                    raise ValueError("Campaign changed; inspect and authorize the current configuration")
                config = Campaign.model_validate_json(row["config"])
                if config.mode != "AUTONOMOUS" or not all([config.sender_name, config.sender_email]):
                    raise ValueError("Configure AUTONOMOUS mode and your complete sender identity first")
                db.execute("update campaigns set approved_hash=config_hash where id=?", (row["id"],))
                self.store.audit("campaign.authorized", row["id"], {"hash": row["config_hash"]}, db)
            return {"authorized": row["id"]}
        if path == "/api/identity":
            allowed = {
                "sender_name",
                "sender_email",
                "postal_address",
                "approved_facts",
                "voice_examples",
                "timezone",
            }
            if set(data) - allowed:
                raise ValueError("Unsupported identity fields")
            campaigns = []
            for row in self.store.all("select id from campaigns"):
                config = app.campaign(row["id"]).model_dump(mode="json")
                config.update(data)
                campaigns.append(Campaign.model_validate(config))
            for config in campaigns:
                app.configure(config)
            return {"saved": True, "message": "Identity saved. Existing unsent drafts require regeneration."}
        if path == "/api/message/approve-all":
            from .outbound import approve, Blocked

            if data.get("confirm_live_sending") is not True or data.get("confirm_us") is not True:
                raise ValueError("Confirm all recipients are in the US and authorize sending the batch")
            if not self.readiness()["ready_to_send"]:
                raise ValueError("Complete sender, model and Google setup before sending")
            approved, skipped = [], []
            with self.store.tx() as db:
                if db.execute("select value from settings where key='paused'").fetchone()[0] == "true":
                    raise ValueError("Sending is paused. Resume sending before approving the batch")
                rows = db.execute(
                    "select m.*,c.email,c.company_id,c.invalid,c.state,c.takeover,c.reply_hold,"
                    "p.paused,p.config from messages m join contacts c on c.id=m.contact_id "
                    "join campaigns p on p.id=m.campaign_id where m.status in ('REVIEW','APPROVED') "
                    "order by m.created_at,m.id"
                ).fetchall()
                for row in rows:
                    reason = None
                    if row["paused"] or json.loads(row["config"])["mode"] == "DRY_RUN":
                        reason = "Campaign is paused or in dry-run mode"
                    elif row["invalid"] or row["state"] in {"SUPPRESSED", "BOUNCED", "LOST", "REJECTED"}:
                        reason = "Contact is invalid, suppressed or closed"
                    elif row["takeover"] or (
                        row["reply_hold"] and row["category"] in {"initial", "followup"}
                    ):
                        reason = "Conversation is on hold or under manual takeover"
                    elif row["state"] == "MEETING_BOOKED" and row["category"] in {"initial", "followup"}:
                        reason = "Meeting already booked"
                    elif db.execute(
                        "select 1 from suppressions where email=? or company_id=?",
                        (row["email"], row["company_id"]),
                    ).fetchone():
                        reason = "Contact is suppressed"
                    if not reason:
                        try:
                            approve(app, row["id"], confirm_us=True, _db=db)
                        except Blocked as exc:
                            reason = str(exc)
                    if reason:
                        skipped.append({"id": row["id"], "email": row["email"], "reason": reason})
                        continue
                    self.store.enqueue("send:" + row["id"], "send", {"message_id": row["id"]}, db=db)
                    # Revive a previously completed/dead send job only for an intact unsent draft.
                    db.execute(
                        "update jobs set status='PENDING',due_at=?,lease_until=NULL,error=NULL "
                        "where id=? and status in ('DONE','DEAD','CANCELLED')",
                        (now().isoformat(), "send:" + row["id"]),
                    )
                    approved.append(row["id"])
                self.record_send_batch(approved, db)
                self.store.audit("messages.batch_approved", "batch", {"count": len(approved)}, db)
            worker, worker_error = None, None
            if approved:
                try:
                    worker = self.worker.start()
                except (OSError, ValueError) as exc:
                    worker_error = str(exc)
            return {"approved": approved, "skipped": skipped, "worker": worker, "worker_error": worker_error}
        if path == "/api/message/approve":
            if not data.get("confirm_live_sending"):
                raise ValueError("Approving queues a real email; explicit confirmation required")
            ids = data.get("ids", [])
            if not ids or len(ids) > 50:
                raise ValueError("Choose 1–50 messages")
            for key in ids:
                app.approve(
                    key,
                    confirm_us=data.get("confirm_us") is True,
                    legal_basis=data.get("legal_basis"),
                    provenance=data.get("provenance"),
                )
                self.store.enqueue("send:" + key, "send", {"message_id": key})
            self.record_send_batch(ids)
            worker, worker_error = None, None
            try:
                worker = self.worker.start()
            except (OSError, ValueError) as exc:
                worker_error = str(exc)
            return {"approved": ids, "worker": worker, "worker_error": worker_error}
        if path == "/api/message/edit":
            from .outbound import edit

            key = edit(app, data.get("id"), data.get("subject"), data.get("body"), data.get("content_hash"))
            return {"saved": True, "id": key}
        if path == "/api/message/reject":
            with self.store.tx() as db:
                db.execute(
                    "update messages set status='CANCELLED' where id=? and status in ('REVIEW','APPROVED','DRY_RUN')",
                    (data["id"],),
                )
                self.store.audit("message.rejected", data["id"], db=db)
            return {"cancelled": data["id"]}
        if path == "/api/prospect/feedback":
            verdict = data.get("verdict")
            if verdict not in {"GOOD", "WRONG_BUYER", "WEAK_REASON", "UNSUITABLE"}:
                raise ValueError("Choose a supported prospect feedback option")
            contact_id = int(data["id"])
            if not self.store.one("select id from contacts where id=?", (contact_id,)):
                raise ValueError("Unknown contact")
            self.store.execute(
                """insert into prospect_feedback values (?,?,?,?) on conflict(contact_id)
                do update set verdict=excluded.verdict,note=excluded.note,updated_at=excluded.updated_at""",
                (contact_id, verdict, str(data.get("note", ""))[:1000], now().isoformat()),
            )
            self.store.audit("prospect.feedback", contact_id, {"verdict": verdict})
            return {"saved": True}
        if path == "/api/prospect/drafts":
            return {"queued": app.queue_automatic_drafts()}
        if path == "/api/prospect/draft":
            contact_id = int(data["id"])
            return self.background_task(
                "Prepare selected prospect draft",
                lambda: {"contact_id": app.prepare_outreach(contact_id)["id"]},
            )
        if path == "/api/prospect/research":
            from .research import LeadResearcher, PublicWeb

            candidate = self.store.one("select * from discovery_candidates where id=?", (int(data["id"]),))
            if not candidate or candidate["contact_id"]:
                raise ValueError("Choose a company still needing contact research")

            def retry_company():
                seed = LeadResearcher(PublicWeb(self.store), app.model).research_company(candidate["url"])
                contact = app.prepare(candidate["campaign_id"], seed)
                self.store.execute(
                    "update discovery_candidates set contact_id=?,status='CONTACT_FOUND',reason='',updated_at=? where id=?",
                    (contact["id"], now().isoformat(), candidate["id"]),
                )
                return {"contact_id": contact["id"]}

            return self.background_task("Research retained company", retry_company)
        if path == "/api/contact/review":
            if data.get("region") != "US":
                raise ValueError("Confirm the recipient is in the US")
            with self.store.tx() as db:
                row = db.execute("select * from contacts where id=?", (int(data["id"]),)).fetchone()
                if not row:
                    raise ValueError("Unknown contact")
                seed = Seed.model_validate_json(row["seed"])
                seed.region, seed.region_reviewed = "US", True
                seed.legal_basis = data.get("legal_basis", seed.legal_basis).strip()
                seed.provenance = data.get("provenance", seed.provenance).strip()
                db.execute("update contacts set seed=? where id=?", (seed.model_dump_json(), row["id"]))
                self.store.audit("contact.reviewed", row["id"], db=db)
            return {"reviewed": row["id"]}
        if path == "/api/contact/takeover":
            with self.store.tx() as db:
                db.execute("update contacts set takeover=1,reply_hold=1 where id=?", (int(data["id"]),))
                db.execute("update followups set status='CANCELLED' where contact_id=?", (int(data["id"]),))
                db.execute(
                    "update messages set status='CANCELLED' where contact_id=? and status in ('REVIEW','APPROVED')",
                    (int(data["id"]),),
                )
                self.store.audit("conversation.takeover", data["id"], db=db)
            return {"taken_over": data["id"]}
        if path == "/api/contact/suppress":
            email = email_address(data["email"])
            with self.store.tx() as db:
                contact = db.execute("select * from contacts where email=?", (email,)).fetchone()
                db.execute(
                    "insert or ignore into suppressions values (?,?,?,?)",
                    (
                        email,
                        contact["company_id"] if contact else None,
                        data.get("reason", "Operator suppression"),
                        now().isoformat(),
                    ),
                )
                if contact:
                    app.transition(contact["id"], "SUPPRESSED", db)
                    db.execute("update followups set status='CANCELLED' where contact_id=?", (contact["id"],))
                self.store.audit("contact.suppressed", email, db=db)
            return {"suppressed": email}
        if path == "/api/research":
            from .research import PublicWeb, LeadResearcher

            app.campaign(data["campaign_id"])
            url = str(data["url"])

            def research():
                seed = LeadResearcher(PublicWeb(self.store), app.model).research(url)
                return {"contact_id": app.prepare(data["campaign_id"], seed)["id"]}

            return self.background_task("Research company and prepare draft", research)
        if path == "/api/import":
            seed = Seed.model_validate(data["seed"])
            app.campaign(data["campaign_id"])
            return self.background_task(
                "Import sourced prospect and prepare draft",
                lambda: {"contact_id": app.prepare(data["campaign_id"], seed)["id"]},
            )
        if path == "/api/discover":
            if (
                self.settings.load().search_provider == "brave"
                and not self.settings.load().brave_api_key
                and not os.environ.get("BRAVE_SEARCH_API_KEY")
            ):
                raise ValueError("Add a Brave API key, or choose free Exa search in setup")
            app.campaign(data["campaign_id"])
            from .research import discover

            return self.background_task("Discover prospects", lambda: discover(app, data["campaign_id"]))
        if path == "/api/redraft":
            return self.background_task(
                "Regenerate outreach under current settings",
                lambda: {"message_id": app.redraft(int(data["id"]))},
            )
        if path == "/api/respond":
            contact = self.store.one("select * from contacts where id=?", (int(data["id"]),))
            if not contact or contact["takeover"] or contact["state"] in {"SUPPRESSED", "LOST", "BOUNCED"}:
                raise ValueError("Contact is closed or under manual takeover")
            latest = self.store.one(
                "select * from inbound where contact_id=? order by received_at desc limit 1", (contact["id"],)
            )
            previous = self.store.one(
                "select * from messages where contact_id=? and status='SENT' order by sent_at desc limit 1",
                (contact["id"],),
            )
            context = {
                "conversation": self.store.all(
                    "select body,classification from inbound where contact_id=?", (contact["id"],)
                ),
                "thread_subject": previous["subject"] if previous else None,
            }

            def respond():
                content = app.writer.compose(
                    app.campaign(contact["campaign_id"]),
                    Seed.model_validate_json(contact["seed"]),
                    "factual_answer",
                    context,
                )
                key = app.queue_message(
                    contact["id"],
                    content,
                    "factual_answer",
                    reply_to=latest["rfc_id"]
                    if latest and latest["rfc_id"]
                    else (previous["rfc_id"] if previous else None),
                )
                return {"message_id": key}

            return self.background_task("Draft grounded response", respond)
        if path == "/api/reconcile":
            from .outbound import reconcile

            return {"status": reconcile(app, data["id"])}
        if path == "/api/job/replay":
            from .worker import Runner

            Runner(app).replay(data["id"])
            return {"replayed": data["id"]}
        if path == "/api/escalation/resolve":
            self.store.execute("update escalations set status='RESOLVED' where id=?", (int(data["id"]),))
            self.store.audit("escalation.resolved", data["id"])
            return {"resolved": data["id"]}
        if path == "/api/worker/start":
            if not self.readiness()["ready_to_research"]:
                raise ValueError("Save and test a model connection and configure the no-ai-slop editor first")
            return self.worker.start()
        if path == "/api/worker/stop":
            return self.worker.stop()
        if path == "/api/poll":
            from .gmail import GmailSync

            return self.background_task(
                "Sync tracked Gmail conversations", lambda: GmailSync(app, app.mail).poll()
            )
        if path in {"/api/slots", "/api/book"}:
            from .calendar import Scheduler, GoogleCalendar
            from .auth import google_client

            scheduler = Scheduler(app, GoogleCalendar(google_client(app.mail.token_file)))
            start = datetime.fromisoformat(data["start"])
            return (
                scheduler.slots(data["campaign_id"], start)
                if path == "/api/slots"
                else scheduler.book(int(data["id"]), start, approved=data.get("confirm_invitation", False))
            )
        if path == "/api/proposal":
            return self.background_task("Prepare scoped proposal", lambda: CRM(app).proposal(int(data["id"])))
        if path == "/api/win":
            return CRM(app).win(
                int(data["id"]), free=data.get("free", False), approved=data.get("confirm_scope", False)
            )
        if path == "/api/project/complete":
            CRM(app).complete(
                int(data["id"]),
                float(data["hours"]),
                float(data["revenue"]),
                float(data.get("cost", 0)),
                str(data.get("customer_value", "")),
                str(data.get("verified_results", "")),
            )
            return {"completed": data["id"]}
        if path == "/api/project/feedback":
            self.store.execute(
                "update projects set testimonial=?,referral=? where id=?",
                (str(data.get("testimonial", "")), str(data.get("referral", "")), int(data["id"])),
            )
            return {"saved": data["id"]}
        if path == "/api/meeting/complete":
            with self.store.tx() as db:
                app.transition(int(data["id"]), "MEETING_COMPLETED", db)
                db.execute("update meetings set status='COMPLETED' where contact_id=?", (int(data["id"]),))
            return {"completed": data["id"]}
        if path == "/api/digest":
            return digest_report(app)
        raise ValueError("Unknown action")

    def oauth_callback(self, query, session):
        state = query.get("state", [""])[0]
        with self.oauth_lock:
            saved = self.oauth_flows.pop(state, None)
        if not saved or saved[0] < time.monotonic() or saved[2] != session:
            raise ValueError("Google connection expired or state is invalid. Start again from setup.")
        if query.get("error") or not query.get("code"):
            raise ValueError("Google authorization was declined. No account connected.")
        flow = saved[1]
        flow.fetch_token(code=query["code"][0])
        save_token(self.app().mail.token_file, flow.credentials.to_json())
        self.store.audit("google.connected", "account")
        # Retrieve account identity only; do not scan a mailbox to test the connection.
        try:
            profile = self.app().mail.get("/profile")
            self.store.execute(
                "insert into sync_state values ('google_account',?) on conflict(key) do update set value=excluded.value",
                (json.dumps({"email": profile.get("emailAddress", "")}),),
            )
        except Exception:
            pass
        return True
