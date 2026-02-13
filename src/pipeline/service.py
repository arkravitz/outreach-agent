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


