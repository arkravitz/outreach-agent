# Ledger — docs/superpowers/plans/2026-10-18-referral-discovery.md

Task 1 complete: persistent query/location/angle rotation, domain exclusions, retry cooldowns and due-company reconsideration, query yields, lease, linked services/team/contact research and targeted same-domain contact search. No guessed addresses or paid fallback.
Task 2 complete: strict compatibility retained; relationship campaigns retain plausible contacts or NEEDS_RESEARCH; drafts selected separately. Operator feedback informs assessment, recipient extraction and similar-company queries. Draft selection persisted by campaign hash and durable preparation job.
Task 3: UI and live configuration loaded; pool target 60 per campaign / 120 total, hourly discovery, existing 100 model-call/day and 10/min caps unchanged, both REVIEW. Database/settings backed up under restricted state/backups before migration. Existing 12 rejected contacts retained: 9 QUALIFIED, 3 NEEDS_RESEARCH. No email sent by implementation.

Independent review: three important retry defects reproduced with failing regression tests and fixed: selection intent lost on retry, temporary-budget candidate cooldown, expired-candidate query exclusion. Optional page budget now preserves already usable research. Related test_research/test_discovery suite 41 passed; ten synthetic browser scenarios passed, including selection/feedback/review-draft/no-send.

Ruling: pool goal 120 total means 60 per default campaign. This is a pool-building goal rather than automatic send authorization; approved sending limits stay unchanged. Cost if distribution is wrong: adjust goals in Campaigns.
Ruling: each pass is capped at four searches and 25 company attempts with shared provider/model budgets. A rate-limited pass schedules continuation after five minutes; daily cap resumes next UTC day. This favors predictable cost over guaranteed per-pass yield.
Ruling: no Git repository; authorized in-place implementation, restricted live backups, no Git commits/worktree scripts.

Final verification: 133 Python tests pass; Ruff lint/format and JavaScript syntax pass. Ten synthetic Chromium scenarios pass after final responsive spacing changes. The live bounded automation search found five new published contacts with zero duplicates from its first query, raising the pool from 13 to 18. It hit the existing minute model limit, persisted a DEFERRED company and scheduled a continuation. The worker was restored with both campaigns REVIEW, no APPROVED messages and zero sent.
