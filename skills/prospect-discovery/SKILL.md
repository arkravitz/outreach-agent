---
name: prospect-discovery
description: "Find sourced businesses for a configured Pipeline campaign."
---

# Prospect Discovery

Run `pipeline discover CAMPAIGN` for a bounded public search pass, or `pipeline schedule-discovery CAMPAIGN` to enqueue it. Inspect returned contact IDs and source URLs. A discovery pass is complete when each accepted lead has a stored source and preparation state; skipped pages appear in audit events. A company with no published address needs another permitted source, not an invented address. Region and source-policy review remain operator decisions before sending.

Run from the Pipeline repository with its virtual environment and the intended PIPELINE_STATE. Core workflows run independently of OpenCode. See [operator setup](../../README.md) for provider configuration and live-action gates.
