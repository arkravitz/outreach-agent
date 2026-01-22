"""SQLite transactions are the authority; Huey wakeups may be delivered repeatedly."""

from contextlib import contextmanager
from pathlib import Path
import json
import logging
import os
import sqlite3
from .models import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
INSERT OR IGNORE INTO settings VALUES ('paused','false');
CREATE TABLE IF NOT EXISTS campaigns (
 id TEXT PRIMARY KEY,config TEXT NOT NULL,config_hash TEXT NOT NULL,approved_hash TEXT,
 paused INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS companies (id INTEGER PRIMARY KEY,domain TEXT UNIQUE NOT NULL,name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contacts (
 id INTEGER PRIMARY KEY,company_id INTEGER REFERENCES companies(id),campaign_id TEXT REFERENCES campaigns(id),
 email TEXT UNIQUE NOT NULL,name TEXT,domain TEXT NOT NULL,state TEXT NOT NULL,seed TEXT NOT NULL,
 score REAL,reason TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 reply_hold INTEGER NOT NULL DEFAULT 0,takeover INTEGER NOT NULL DEFAULT 0,invalid INTEGER NOT NULL DEFAULT 0,
 thread_id TEXT UNIQUE,followup_count INTEGER NOT NULL DEFAULT 0,variant TEXT);
CREATE TABLE IF NOT EXISTS suppressions (
 email TEXT PRIMARY KEY,company_id INTEGER,reason TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
 id TEXT PRIMARY KEY,contact_id INTEGER REFERENCES contacts(id),campaign_id TEXT NOT NULL,
 category TEXT NOT NULL,subject TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,
 config_hash TEXT NOT NULL,content_hash TEXT NOT NULL,approved_hash TEXT,
 skill_hash TEXT NOT NULL,edit_audit TEXT NOT NULL,created_at TEXT NOT NULL,due_at TEXT NOT NULL,
 sent_at TEXT,gmail_id TEXT UNIQUE,thread_id TEXT,rfc_id TEXT UNIQUE NOT NULL,
 error TEXT,attempts INTEGER NOT NULL DEFAULT 0,reply_to TEXT);
CREATE INDEX IF NOT EXISTS messages_due ON messages(status,due_at);
CREATE TABLE IF NOT EXISTS inbound (
 id TEXT PRIMARY KEY,contact_id INTEGER,thread_id TEXT,body TEXT NOT NULL,
 sender TEXT,received_at TEXT NOT NULL,classification TEXT,confidence REAL,rfc_id TEXT);
CREATE TABLE IF NOT EXISTS followups (
 id INTEGER PRIMARY KEY,contact_id INTEGER NOT NULL,ordinal INTEGER NOT NULL,
 due_at TEXT NOT NULL,status TEXT NOT NULL,UNIQUE(contact_id,ordinal));
CREATE TABLE IF NOT EXISTS executions (
 id TEXT PRIMARY KEY,contact_id INTEGER,step TEXT NOT NULL,status TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0,result TEXT,error TEXT,next_at TEXT,
 updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,
 due_at TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,lease_until TEXT,error TEXT);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY,event TEXT NOT NULL,entity TEXT NOT NULL,data TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS escalations (
 id INTEGER PRIMARY KEY,contact_id INTEGER,reason TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'OPEN',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meetings (
 id TEXT PRIMARY KEY,contact_id INTEGER NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,
 event_id TEXT UNIQUE NOT NULL,status TEXT NOT NULL,brief TEXT);
CREATE TABLE IF NOT EXISTS opportunities (
 id INTEGER PRIMARY KEY,contact_id INTEGER UNIQUE NOT NULL,status TEXT NOT NULL,
 proposal TEXT,proposal_approved_hash TEXT,value REAL DEFAULT 0,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects (
 id INTEGER PRIMARY KEY,opportunity_id INTEGER UNIQUE NOT NULL,status TEXT NOT NULL,
 free INTEGER NOT NULL,hours REAL DEFAULT 0,revenue REAL DEFAULT 0,cost REAL DEFAULT 0,
 customer_value TEXT,verified_results TEXT,testimonial TEXT,referral TEXT,completed_at TEXT);
CREATE TABLE IF NOT EXISTS experiments (
 company_id INTEGER PRIMARY KEY,campaign_id TEXT NOT NULL,variant TEXT NOT NULL,assigned_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS discovery_candidates (
 id INTEGER PRIMARY KEY,campaign_id TEXT NOT NULL,domain TEXT NOT NULL,url TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'NEW',reason TEXT NOT NULL DEFAULT '',query TEXT NOT NULL DEFAULT '',
 attempts INTEGER NOT NULL DEFAULT 0,next_at TEXT,contact_id INTEGER,updated_at TEXT NOT NULL,
 UNIQUE(campaign_id,domain));
CREATE TABLE IF NOT EXISTS discovery_queries (
 id INTEGER PRIMARY KEY,campaign_id TEXT NOT NULL,query TEXT NOT NULL,
 candidates INTEGER NOT NULL DEFAULT 0,contacts INTEGER NOT NULL DEFAULT 0,
 duplicates INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prospect_feedback (
 contact_id INTEGER PRIMARY KEY REFERENCES contacts(id),verdict TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',
 updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_cache (url TEXT PRIMARY KEY,text TEXT NOT NULL,fetched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS provider_events (id INTEGER PRIMARY KEY,provider TEXT NOT NULL,at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS provider_events_lookup ON provider_events(provider,at);
CREATE TABLE IF NOT EXISTS provider_calls (provider TEXT PRIMARY KEY,last_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_usage (
 id INTEGER PRIMARY KEY,task TEXT NOT NULL,model TEXT NOT NULL,endpoint TEXT NOT NULL,
 status TEXT NOT NULL,input_tokens INTEGER NOT NULL DEFAULT 0,output_tokens INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS model_usage_day ON model_usage(created_at);
PRAGMA user_version=1;
"""


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript(SCHEMA)
            if "rfc_id" not in {row[1] for row in db.execute("pragma table_info(inbound)")}:
                db.execute("alter table inbound add column rfc_id TEXT")
        os.chmod(self.path, 0o600)
        self.update_opt_out_copy()

    def update_opt_out_copy(self):
        """Replace only known footer text in intact, unsent drafts without inference."""
        from .outbound import content_hash
        from .writing import LEGACY_OPT_OUT, OPT_OUT

        with self.tx() as db:
            rows = db.execute(
                "select * from messages where status in ('REVIEW','APPROVED','DRY_RUN') and instr(body,?)>0",
                (LEGACY_OPT_OUT,),
            ).fetchall()
            for row in rows:
                if row["content_hash"] != content_hash(row):
                    continue
                message = dict(row)
                message["body"] = row["body"].replace(LEGACY_OPT_OUT, OPT_OUT)
                status = "REVIEW" if row["status"] == "APPROVED" else row["status"]
                db.execute(
                    "update messages set body=?,content_hash=?,approved_hash=NULL,status=? where id=?",
                    (message["body"], content_hash(message), status, row["id"]),
                )
                self.audit("message.opt_out_updated", row["id"], db=db)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=45, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("pragma journal_mode=WAL")
        db.execute("pragma foreign_keys=ON")
        db.execute("pragma busy_timeout=45000")
        return db

    @contextmanager
    def tx(self):
        db = self.connect()
        try:
            db.execute("begin immediate")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def one(self, sql, args=()):
        with self.connect() as db:
            row = db.execute(sql, args).fetchone()
            return dict(row) if row else None

    def all(self, sql, args=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.tx() as db:
            return db.execute(sql, args).lastrowid

    def audit(self, event, entity, data=None, db=None):
        values = (event, str(entity), json.dumps(data or {}, default=str), now().isoformat())
        if db is not None:
            db.execute("insert into audit(event,entity,data,created_at) values (?,?,?,?)", values)
        else:
            self.execute("insert into audit(event,entity,data,created_at) values (?,?,?,?)", values)
        logging.getLogger("pipeline").info(json.dumps({"event": event, "entity": str(entity)}))

    def escalate(self, contact_id, reason, db=None):
        sql = "insert into escalations(contact_id,reason,created_at) values (?,?,?)"
        args = (contact_id, reason, now().isoformat())
        if db is not None:
            db.execute(sql, args)
        else:
            self.execute(sql, args)

    def enqueue(self, job_id, kind, payload, due_at=None, db=None):
        args = (job_id, kind, json.dumps(payload), "PENDING", (due_at or now()).isoformat())
        sql = "insert or ignore into jobs(id,kind,payload,status,due_at) values (?,?,?,?,?)"
        if db is not None:
            db.execute(sql, args)
        else:
            self.execute(sql, args)
