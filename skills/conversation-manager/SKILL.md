---
name: conversation-manager
description: "Review Pipeline conversation context and draft a grounded next response."
---

# Conversation Manager

Use `pipeline inspect CONTACT_ID`, including incoming text, prior outbound messages and current state. Use `pipeline respond CONTACT_ID` to draft an answer grounded in approved facts. Scope/pricing/negotiation or work commitments need user review; preserve that requirement when choosing the message category. `pipeline takeover CONTACT_ID` stops automatic sends while the human manages the thread. Complete with a queued response or a clear escalation; keep follow-up cancellation intact.

Run from the Pipeline repository with its virtual environment and the intended PIPELINE_STATE. Core workflows run independently of OpenCode. See [operator setup](../../README.md) for provider configuration and live-action gates.
