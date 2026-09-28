---
name: meeting-scheduler
description: "Offer fresh Google Calendar slots or book an explicitly agreed Pipeline meeting."
---

# Meeting Scheduler

Use `pipeline slots CAMPAIGN --start ISO_WITH_TIMEZONE` for fresh availability. `pipeline respond CONTACT_ID --category scheduling` drafts a reply using fresh slots. Once the exact time has been mutually agreed and the user authorizes the invitation, use `pipeline book CONTACT_ID --start ISO_WITH_TIMEZONE --approve-invitation`. This creates a real invitation. If time or timezone is ambiguous, escalate. Complete only with recorded event ID/start/end and a meeting state, or report a conflict. Follow-ups remain cancelled.

Run from the Pipeline repository with its virtual environment and the intended PIPELINE_STATE. Core workflows run independently of OpenCode. See [operator setup](../../README.md) for provider configuration and live-action gates.
