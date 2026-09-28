---
name: outreach-writer
description: "Draft grounded Pipeline outreach and queue it under campaign controls."
---

# Outreach Writer

Prepare sourced contact JSON with `pipeline prepare CAMPAIGN FILE`. For an existing unsent draft after a configuration change use `pipeline redraft CONTACT_ID`. The finite preparation workflow calls the writer, installed no-ai-slop editor, quality validator and grounding auditor before queueing. Inspect the draft and research with `pipeline inspect`. The skill ends at the queue; separate authorization is required to approve or send. Preserve hypotheses as questions and use only approved sender credentials and bounded offers.

Run from the Pipeline repository with its virtual environment and the intended PIPELINE_STATE. Core workflows run independently of OpenCode. See [operator setup](../../README.md) for provider configuration and live-action gates.
