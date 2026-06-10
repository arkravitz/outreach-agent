from pathlib import Path
import os
import pytest
from pipeline.db import Store
from pipeline.models import Campaign, Seed, Evidence
from pipeline.service import Pipeline

SKILL = Path(os.environ.get("NO_AI_SLOP_DIR", Path.home() / ".agents/skills/no-ai-slop"))
SKILL_IS_STUB = not ((SKILL / "SKILL.md").exists() and (SKILL / "eval.md").exists())
if not (SKILL / "SKILL.md").exists() or not (SKILL / "eval.md").exists():
    import tempfile

    SKILL = Path(tempfile.mkdtemp(prefix="no-ai-slop-"))
    (SKILL / "SKILL.md").write_text(
        "---\nname: no-ai-slop\ndescription: Rewrite draft copy so it reads plainly.\n---\n\n"
        "# no-ai-slop\n\nWrite plainly and specifically. Prefer concrete detail over rhetoric.\n"
    )
    (SKILL / "eval.md").write_text("# eval\n\n- no em dashes\n- no hype adjectives\n- no throat clearing\n")


class Model:
    def __init__(self):
        self.calls = []
        self.bad = False

    def complete(self, task, payload):
        self.calls.append((task, payload))
        if task == "qualify":
            return {
                "score": 0.9 if "documents" in str(payload["recipient"]["evidence"]).lower() else 0.1,
                "reason": "Document workflow fit",
                "confidence": 0.95,
            }
        if task == "write":
            if payload.get("category") == "followup":
                return {
                    "subject": "Document workflow question",
                    "body": "Hi Sam,\n\nFor a first conversation, we could map how one document moves from arrival to approval before choosing any tools. Would that be useful?",
                }
            return {
                "subject": "Document workflow question",
                "body": "Hi Sam,\n\nYour site says you process customer documents. Is reviewing those documents taking time away from client work? Would a short conversation help?",
            }
        if task == "edit":
            return {
                "body": payload["draft"],
                "changes": "Kept the specific detail and direct question.",
                "checks_passed": True,
            }
        if task == "ground":
            return {
                "supported": not self.bad,
                "confidence": 0.98,
                "unsupported_claims": ["invented claim"] if self.bad else [],
            }
        if task == "classify":
            return {"category": "UNCERTAIN", "confidence": 0.4, "reason": "Ambiguous"}
        if task == "proposal":
            return {
                "scope": "Review one document workflow",
                "assumptions": ["Customer supplies sample data"],
                "deliverables": ["Written findings"],
                "exclusions": ["Production deployment"],
                "hours": 2,
            }
        raise AssertionError(task)


class Mail:
    def __init__(self):
        self.sent = []
        self.error = None
        self.found = None

    def send(self, message):
        if self.error:
            raise self.error
        self.sent.append(message)
        return {"id": "gm-" + message["id"], "threadId": "thread-" + str(message["contact_id"])}

    def find_sent(self, rfc_id):
        return self.found


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "pipeline.db")
    campaign = Campaign(
        id="automation",
        name="AI Automation",
        keywords=["documents"],
        offer="A free assessment of one document workflow, limited to two hours.",
        offer_hours=2,
        sender_name="Aaron",
        sender_email="aaron@example.com",
        postal_address="123 Example St, San Francisco, CA 94101",
        approved_facts=["I build AI tools."],
        allowed_regions=["US"],
    )
    model = Model()
    mail = Mail()
    app = Pipeline(store, model, mail, skill_dir=SKILL)
    app.configure(campaign)
    return app, model, mail, campaign


@pytest.fixture
def seed():
    return Seed(
        company="Example Agency",
        domain="example.org",
        email="sam@example.org",
        name="Sam",
        role="Owner",
        region="US",
        region_reviewed=True,
        legal_basis="US commercial outreach; business relevance reviewed",
        provenance="Company contact page",
        source_url="https://example.org/contact",
        verification="published",
        evidence=[
            Evidence(
                url="https://example.org/services",
                quote="We process customer documents.",
                text="We process customer documents. Contact Sam, Owner.",
            )
        ],
    )
