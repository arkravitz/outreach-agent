---
name: reply-classifier
description: "Classify a stored Pipeline reply and inspect the resulting sales action."
---

# Reply Classifier

Run `pipeline poll` to synchronize Gmail, then inspect `pipeline conversations` and `pipeline escalations`. A manually imported supported provider event uses `pipeline ingest FILE`; classification is `pipeline classify GMAIL_MESSAGE_ID`. Ingestion cancels pending follow-ups before model work. Explicit opt-outs suppress immediately. Low-confidence, ambiguous, scope/pricing and unusual questions remain for review. Completion requires a persisted classification and the corresponding hold/close/suppression/escalation; an unclassified email is not permission to follow up.

Run from the Pipeline repository with its virtual environment and the intended PIPELINE_STATE. Core workflows run independently of OpenCode. See [operator setup](../../README.md) for provider configuration and live-action gates.
