# Prompt and throughput verification — 2026-10-18

User requested substantially higher limits and minimal prompts for email drafting and prospect review. All changes preserve explicit approval, source/location review, profile exclusions, factual grounding and correspondence privacy choices.

A same-record comparison measured instruction + JSON payload characters (not a guaranteed token/billing reduction):

| Task | Before | After |
| --- | ---: | ---: |
| Draft | 6,169 | 1,859 |
| Edit | 14,729 | 5,326 |
| Ground | 5,821 | 1,906 |
| Qualify | 5,709 | 1,102 |

Combined reduction is approximately 69%. Source pages remain available locally. Research excerpts preserve exact strings, prioritize distinct suitable email addresses, and include nearby identity/business context. Sender fact selection copies at most three approved facts verbatim; operational campaign settings are omitted from model input, while offer hours and bounded terms remain.

The compact editor profile is derived from the installed skill's principle names, word rules and its actual complete eval.md. Full source hashes remain in audit records; unknown skill formats use full sources. Editing audit notes returned as a list of strings are normalized without generating another draft. This does not relax body, factual or quality validation.

A live Gemini review draft completed successfully with 492 writing input tokens, 1,387 editing input tokens and 504 grounding input tokens. The final saved email had 111 words including deterministic footer. No email was sent. A prior live edit exposed list-shaped audit notes; regression coverage was added before the compatibility fix. Model-output truncation now has bounded larger retries; every request reserves a separate call and records tokens. Per-task limits are configurable in the UI.

Higher live limits: 1,000 calls/day; 60 calls/minute; 50 contacts/pass; 12 searches and 100 company checks/pass; 20 search requests/minute; 240 page fetches/minute globally; 20/company/minute. Approved-send pacing: 25/campaign/day, 50/account/day, 2/company/day and minimum 120 seconds. No send authorization is added. Source policies/provider quotas still apply. The live database and settings were backed up under restricted state/backups before applying changes. Raising caps immediately wakes budget-deferred work.

Independent review identified contact omission after repeated unsuitable addresses and hard response caps; both were reproduced and fixed. Regression/browser tests cover compact task contexts, actual skill-derived checks, exact long-page tail contacts, usable-address priority, truncation retries/accounting, adjustable response caps, budget wake-up and settings persistence.
