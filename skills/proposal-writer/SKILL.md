---
name: proposal-writer
description: "Draft a bounded proposal from a Pipeline conversation for human scope approval."
---

# Proposal Writer

Use `pipeline proposal CONTACT_ID` for an engaged prospect or completed meeting. Inspect scope, assumptions, deliverables, exclusions and hours. Proposal creation stores an internal draft, not a sent proposal. User approval is required for price, custom scope, contract language and work commitments. `pipeline win CONTACT_ID --approve-scope` records a real user-confirmed agreement; add --free only for an approved bounded free offer. Completion means a stored proposal draft with review required, or a recorded user-confirmed outcome.

Run from the Pipeline repository with its virtual environment and the intended PIPELINE_STATE. Core workflows run independently of OpenCode. See [operator setup](../../README.md) for provider configuration and live-action gates.
