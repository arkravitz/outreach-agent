# Prospect review and automatic drafting

User requested clickable prospect review, cleaner layout and automatically creating emails above a threshold. Implemented directly as an authorized correction to the previous workflow.

Decision: automatic drafts at >=50% fit by default, configurable per campaign. Retain existing 100 model calls/day and 10/min caps, source/location checks and per-email approval. No change to daily send limits or autonomy mode.

Reproductions: a company cell click did not open review; only the off-to-right Inspect button worked. Browser test now opens the prospect from a cell at phone width. Existing modal auto-focus also scrolled to the lower feedback field; focus now avoids scrolling. The new table uses mobile cards and visible review actions. A progress bar inherited horizontal margins and overflowed its card; scoped margins were corrected.

Automatic drafting: eligible new prospects draft during preparation; existing prospects get idempotent durable jobs keyed by campaign hash. Stale existing drafts regenerate under current settings, but discarded emails are not resurrected. Draft work runs before additional discovery, including persisted discovery jobs from prior workers. Model limits preserve pending jobs and resume after budget reset.

Independent review: legacy campaign fields/hash mismatch reproduced and fixed by conditional canonical migration with autonomous authorization cleared; disabling automatic drafts after queueing is respected; discarding during composition prevents replacement; sent emails do not route into an unrelated review inbox item. Author confirmed DRY_RUN is already included in pending(), so that reviewer claim needed no change.

Live: backed up database under restricted state/backups, enabled automatic drafting at .5 on both existing REVIEW campaigns and queued 27 eligible initial jobs. Existing runtime cost caps preserved. Worker restarted for final code.

Final checks: 143 Python tests pass; lint, formatting and JavaScript syntax pass. Browser regression now covers row clicks at phone width, visible Review buttons, threshold save, fresh inline preview after background drafting, direct exact-message inbox navigation, and no review action for a sent email. Live click-through on an automatically generated draft verified its matching recipient and subject. Generation remains REVIEW-only and zero emails have been sent.
