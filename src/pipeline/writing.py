"""All outbound content goes through the installed editor and final grounding."""

from pathlib import Path
import re
from .models import Draft, Edit, Grounding, digest
from .prompt_context import writing_campaign, editor_profile


OPT_OUT = "Let me know if you want me to stop contacting you."
LEGACY_OPT_OUT = 'Reply "unsubscribe" to stop all outreach.'


class WritingError(ValueError):
    pass


class Writer:
    def __init__(self, model, skill_dir, excluded_terms=None):
        self.excluded_terms = excluded_terms or []
        self.model = model
        self.skill_dir = Path(skill_dir).expanduser()

    def compose(self, campaign, seed, category="initial", context=None):
        try:
            skill = (self.skill_dir / "SKILL.md").read_text()
            evaluation = (self.skill_dir / "eval.md").read_text()
        except OSError as exc:
            raise WritingError(
                "Required no-ai-slop SKILL.md and eval.md missing; see README installation"
            ) from exc
        if "name: no-ai-slop" not in skill:
            raise WritingError("Configured editor is not the no-ai-slop skill")
        compact_skill, compact_evaluation = editor_profile(skill, evaluation)
        payload = {
            "campaign": writing_campaign(campaign, seed),
            "recipient": seed.research_context(),
            "category": category,
            **({"context": context} if context else {}),
            "untrusted_data": True,
        }
        draft = Draft.model_validate(self.model.complete("write", payload))
        self.check_profile(draft.subject, draft.body)
        if context and context.get("thread_subject"):
            draft.subject = context["thread_subject"]
        edit = Edit.model_validate(
            self.model.complete(
                "edit",
                {
                    "skill": compact_skill,
                    "evaluation": compact_evaluation,
                    "draft": draft.body,
                    "voice_examples": payload["campaign"].get("voice_examples", []),
                    "untrusted_data": True,
                },
            )
        )
        if not edit.checks_passed:
            raise WritingError("no-ai-slop evaluation failed")
        body = edit.body.strip()
        self.check_profile(draft.subject, body)
        self.quality(draft.subject, body)
        if category == "followup":
            normalized = " ".join(body.lower().split())
            if any(
                normalized in " ".join(m["body"].lower().split())
                for m in (context or {}).get("previous_messages", [])
            ):
                raise WritingError("Follow-up repeats a previous email; add useful context")
        grounding = Grounding.model_validate(
            self.model.complete(
                "ground",
                {
                    **payload,
                    "subject": draft.subject,
                    "body": body,
                },
            )
        )
        if (
            not grounding.supported
            or grounding.unsupported_claims
            or grounding.confidence < campaign.confidence_threshold
        ):
            raise WritingError("Final grounding rejected unsupported or uncertain claims")
        footer = (
            "\n\n"
            + "\n".join(part for part in [campaign.sender_name, campaign.postal_address.strip()] if part)
            + "\n"
            + "Business solicitation. "
            + OPT_OUT
        )
        self.check_profile(draft.subject, body + footer)
        return {
            "campaign_hash": digest(campaign.model_dump(mode="json")),
            "subject": draft.subject,
            "body": body + footer,
            "skill_hash": digest(skill + evaluation),
            "edit_audit": {
                "changes": edit.changes,
                "checks_passed": True,
                "grounding": grounding.model_dump(),
                "skill_path": str(self.skill_dir),
                "editor_profile_hash": digest(compact_skill + compact_evaluation),
                "editor_profile_characters": len(compact_skill + compact_evaluation),
                "draft_hash": digest(draft.body),
                "edited_hash": digest(body),
            },
        }

    def check_profile(self, subject, body):
        for term in self.excluded_terms:
            if term.strip() and re.search(
                r"(?<!\w)" + re.escape(term.strip()) + r"(?!\w)", subject + "\n" + body, re.I
            ):
                raise WritingError(
                    "Email conflicts with saved profile preferences; regenerate with approved facts only"
                )

    @staticmethod
    def quality(subject, body):
        if any(c in subject for c in "\r\n") or subject.lower().startswith(("re:", "fwd:")):
            raise WritingError("Misleading or invalid subject")
        if "\u2014" in body or len(body.split()) > 180 or body.count("?") > 2:
            raise WritingError("Email must be short, use one request and no em dashes")
        banned = r"\b(delve|leverage|synergy|game changer|streamline|cutting-edge|empower|foster|utilize)\b"
        if re.search(banned, body, re.I) or re.search(r"hope this (email|message) finds you", body, re.I):
            raise WritingError("Email failed final language quality checks")
        if re.search(r"(?im)^\s*(#{1,6} |[-*] )|\*\*", body):
            raise WritingError("Plain text required")
