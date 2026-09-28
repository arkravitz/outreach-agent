---
name: outreach-editor
description: "Apply the installed no-ai-slop skill within Pipeline outbound drafting."
---

# Outreach Editor

The runtime Writer.compose loads NO_AI_SLOP_DIR/SKILL.md and eval.md, passes their full contents to the configured editor model, and records their hash plus What changed and evaluation outcome. Use `pipeline redraft CONTACT_ID` for an unsent draft to execute this path. Inspect edit_audit and skill_hash in the stored message. If the actual dependency is absent, follow the installation section in ../../README.md and stop draft creation. A regex cleanup is not a substitute. Complete only when editor evaluation and final grounding pass.

Run from the Pipeline repository with its virtual environment and the intended PIPELINE_STATE. Core workflows run independently of OpenCode. See [operator setup](../../README.md) for provider configuration and live-action gates.
