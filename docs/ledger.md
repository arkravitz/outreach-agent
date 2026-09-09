# Execution ledger

- Environment: empty directory, no Git checkout, no existing database/deployment. Actual no-ai-slop SKILL.md/eval.md present. Calendar desktop connector present, no Gmail connector or standalone tokens.
- Decisions: follow user's instruction to choose architecture and build inline; no additional architectural approval gate. SQLite/Huey single-host worker with transactional outbox; mock external operations. Review default, no autonomous activation.
- Slice 1: failing preparation tests, then 8 passing (qualification/rejection/dedupe/editor/grounding/skill/evidence).
- Slice 2: failing outbound/reply/API tests, then 37 passing (send reservation, approvals, suppression, concurrency, reply state and checkpoints).
- Slice 3: failing Calendar/worker tests, then 55 passing including CRM/research adapter contracts.
- Slice 4: CLI, config, host sample, actual Huey integration and lifecycle demonstration added under tests. No services installed or real messages sent.
- Independent review: three Important findings reproduced red, fixed green: new-thread RFC matching/recovery, machine-readable DSNs, booking state refresh inside transaction. RFC reply headers and subject preservation also fixed with regressions.
- Final usability: persistent per-provider budgets, stable company assignments, unsent redraft path, repeat-followup rejection and documentation added with tests.
- No deferred minor review findings. Live credential validation and manual negotiation/meeting agreement are documented product boundaries, not hidden completion claims.

## Frontend follow-up

- User requested a full operator/setup UI and the cheapest practical model setup; clarified AI Studio/Gemini API credits and 16–32 GB RAM.
- Built loopback-only browser app, saved runtime settings, OAuth client upload/browser flow, Gemini/Ollama presets, model testing/listing, explicit local model download/context setup, budgets/token telemetry, worker supervision and domain-action screens.
- Independent review findings fixed with regressions: stale owned-worker heartbeat and duplicate concurrent preparation/model calls. Further tests bind draft generation to its campaign configuration; browser form ID clobbering fixed and verified in Chromium.
- Actual inventory: 16 GB RAM, existing qwen3.5:9b. Local synthetic JSON qualification passed in 20.9 seconds with thinking disabled after the initial thinking-enabled timeout. No cloud credits or real email/calendar side effects.
- 90 Python tests and five Chromium scenarios pass; final checks recorded in docs/frontend-verification.md. Model prose quality and Google account acceptance remain explicitly unclaimed.
