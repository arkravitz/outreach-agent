"""Validated campaign and evidence boundaries. Model instructions never set policy."""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
import hashlib
import json
import re
from pydantic import BaseModel, ConfigDict, Field, field_validator


def now():
    return datetime.now(timezone.utc)


def digest(value):
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(value.encode()).hexdigest()


def email_address(value):
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}", value):
        raise ValueError("Invalid email address")
    return value


class Mode(StrEnum):
    DRY_RUN = "DRY_RUN"
    REVIEW = "REVIEW"
    AUTONOMOUS = "AUTONOMOUS"


class Campaign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    name: str
    mode: Mode = Mode.REVIEW
    keywords: list[str] = Field(min_length=1)
    discovery_queries: list[str] = []
    prospecting_goal: Literal["PAID_FIT", "RELATIONSHIPS"] = "PAID_FIT"
    pool_target: int = Field(default=120, ge=1, le=10000)
    auto_draft: bool = False
    auto_draft_threshold: float = Field(default=0.5, ge=0, le=1)
    offer: str = Field(min_length=10)
    offer_hours: float = Field(gt=0, le=16)
    max_free_projects: int = Field(default=3, ge=0, le=100)
    sender_name: str = ""
    sender_email: str = ""
    postal_address: str = ""
    approved_facts: list[str] = []
    voice_examples: list[str] = []
    allowed_regions: list[str] = ["US"]
    daily_limit: int = Field(default=25, ge=1, le=50)
    global_daily_limit: int = Field(default=50, ge=1, le=100)
    domain_daily_limit: int = Field(default=2, ge=1, le=2)
    min_send_interval_seconds: int = Field(default=120, ge=60)
    followup_business_days: list[int] = [4, 7]
    qualification_threshold: float = Field(default=0.75, ge=0, le=1)
    confidence_threshold: float = Field(default=0.9, ge=0.8, le=1)
    autonomous_categories: list[str] = ["initial", "followup"]
    timezone: str = "America/Los_Angeles"
    working_start: int = Field(default=9, ge=0, le=23)
    working_end: int = Field(default=17, ge=1, le=24)
    meeting_minutes: int = Field(default=30, ge=15, le=120)
    calendar_id: str = "primary"
    blocked_domains: list[str] = []
    experiment_variants: list[str] = []

    @field_validator("sender_email")
    @classmethod
    def sender(cls, value):
        return email_address(value) if value else value

    @field_validator("allowed_regions")
    @classmethod
    def regions(cls, value):
        if value != ["US"]:
            raise ValueError("MVP policy supports only US; other regions require a reviewed implementation")
        return value

    @field_validator("followup_business_days")
    @classmethod
    def followups(cls, value):
        if len(value) > 2 or any(n < 1 or n > 60 for n in value):
            raise ValueError("At most two follow-ups, each 1..60 business days")
        return value

    @field_validator("autonomous_categories")
    @classmethod
    def categories(cls, value):
        if set(value) - {"initial", "followup", "scheduling", "factual_answer"}:
            raise ValueError("Commitments are never autonomous")
        return value

    @field_validator("timezone")
    @classmethod
    def zone(cls, value):
        from zoneinfo import ZoneInfo

        ZoneInfo(value)
        return value


class Evidence(BaseModel):
    url: str
    quote: str = Field(min_length=10, max_length=1500)
    text: str = Field(min_length=10, max_length=100000)
    fetched_at: datetime = Field(default_factory=now)
    verified: bool = True


class Seed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str = Field(min_length=1, max_length=200)
    domain: str = Field(pattern=r"^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
    email: str
    name: str = ""
    role: str = ""
    region: str = "UNKNOWN"
    region_reviewed: bool = False
    legal_basis: str = ""
    provenance: str = ""
    source_url: str
    verification: str = "unverified"
    evidence: list[Evidence] = Field(min_length=1)

    def research_context(self):
        # Only exact evidence and recipient identity are useful to these model tasks.
        # Source-policy, geography, timestamps and full pages stay in the local record.
        return {
            "company": self.company,
            "name": self.name,
            "role": self.role,
            "email": self.email,
            "evidence": [{"url": e.url, "quote": e.quote} for e in self.evidence],
        }

    @field_validator("domain")
    @classmethod
    def canonical_domain(cls, value):
        return value.lower().removeprefix("www.")

    @field_validator("email")
    @classmethod
    def address(cls, value):
        return email_address(value)


class Qualification(BaseModel):
    score: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    reason: str


class Draft(BaseModel):
    subject: str = Field(min_length=3, max_length=120)
    body: str = Field(min_length=20, max_length=4000)


class Edit(BaseModel):
    body: str = Field(min_length=20, max_length=4000)
    changes: str
    checks_passed: bool

    @field_validator("changes", mode="before")
    @classmethod
    def audit_notes(cls, value):
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return "; ".join(value)
        return value


class Grounding(BaseModel):
    supported: bool
    confidence: float = Field(ge=0, le=1)
    unsupported_claims: list[str]


class Classification(BaseModel):
    category: str
    confidence: float = Field(ge=0, le=1)
    reason: str
