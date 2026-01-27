"""Small task-specific model contexts; canonical policy and full sources stay local."""

import re


def prospect_campaign(campaign):
    data = campaign.model_dump(mode="json") if hasattr(campaign, "model_dump") else (campaign or {})
    return {
        key: data[key]
        for key in ("name", "prospecting_goal", "keywords", "offer", "offer_hours")
        if data.get(key)
    }


def writing_campaign(campaign, seed):
    context = prospect_campaign(campaign)
    # Select exact approved facts locally, rather than repeatedly sending the whole CV.
    terms = set(
        re.findall(
            r"[a-z]{4,}",
            " ".join(
                [seed.company, seed.role, *campaign.keywords, *[e.quote for e in seed.evidence]]
            ).lower(),
        )
    )
    ranked = sorted(
        enumerate(campaign.approved_facts),
        key=lambda item: (-len(terms & set(re.findall(r"[a-z]{4,}", item[1].lower()))), item[0]),
    )
    context["approved_facts"] = [fact for _, fact in ranked[:3]]
    if campaign.voice_examples:
        context["voice_examples"] = [text[:500] for text in campaign.voice_examples[:2]]
    return context


def feedback_context(rows):
    return [{"verdict": r["verdict"], "note": r.get("note", "")[:240]} for r in rows[:3]]


def editor_profile(skill, evaluation):
    """Use the installed skill's rule names, word rules and complete edit checklist.

    The checklist is the actual skill's operational evaluation, not a substitute
    banned-word heuristic. Interactive instructions/examples are unnecessary here.
    Unknown formats fall back to full sources to preserve custom rules.
    """
    try:
        known = {
            "Two jobs",
            "What to ask for",
            "Editing principles",
            "Words to cut",
            "Patterns to cut",
            "Workflow",
        }
        if set(re.findall(r"^## (.+)$", skill, re.M)) - known:
            return skill, evaluation
        principles = skill.split("## Editing principles\n", 1)[1].split("\n## ", 1)[0]
        words = skill.split("## Words to cut\n", 1)[1].split("\n## ", 1)[0]
        names = re.findall(r"^- \*\*(.+?)\*\*", principles, re.M)
        if not names or not all(
            heading in evaluation
            for heading in ("## Editing principles", "## Patterns to cut", "## Final read")
        ):
            return skill, evaluation
        return "name: no-ai-slop\nInstalled editing principles:\n" + "\n".join(
            names
        ) + "\n\n## Words to cut\n" + words, evaluation
    except IndexError:
        return skill, evaluation


def research_excerpts(body, keywords=()):
    """Exact source slices around identity, business terms and published addresses."""
    windows = [(0, min(900, len(body)))]
    emails = list(re.finditer(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", body))
    unique = set()
    for match in emails:
        address = match.group().lower()
        if address in unique or not suitable_business_email(address):
            continue
        unique.add(address)
        windows.append((max(0, match.start() - 160), min(len(body), match.end() + 180)))
        if len(unique) >= 8:
            break
    terms = list(dict.fromkeys([*keywords, "provide", "services", "founder", "owner", "develop", "workflow"]))
    for term in terms[:10]:
        match = re.search(r"\b" + re.escape(term) + r"\b", body, re.I)
        if match:
            windows.append((max(0, match.start() - 100), min(len(body), match.end() + 220)))
    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return "\n[…]\n".join(body[start:end] for start, end in merged)


TASK_OUTPUT_CAPS = {
    "qualify": 384,
    "write": 768,
    "edit": 768,
    "ground": 512,
    "research": 768,
    "classify": 384,
    "proposal": 2048,
}


def suitable_business_email(email):
    local = email.lower().split("@")[0]
    return "@" in email and not any(
        word in local
        for word in (
            "hiring",
            "career",
            "jobs",
            "recruit",
            "privacy",
            "legal",
            "abuse",
            "noreply",
            "no-reply",
            "security",
            "webmaster",
        )
    )
