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


