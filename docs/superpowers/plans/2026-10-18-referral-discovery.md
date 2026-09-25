# Referral Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a diverse, reviewable prospect pool toward 120 contacts for bounded free help and teaching.
**Architecture:** Persist discovery candidates and query history in SQLite. Enrich company evidence with bounded link-following, keep a separate relationship qualification policy, expose research/feedback/progress in the existing UI.
**Tech Stack:** Python, SQLite, dependency-free JavaScript, free Exa, existing Gemini model budget.
**Spec:** docs/superpowers/specs/2026-10-18-referral-discovery-design.md

## Global Constraints
- No automatic paid fallback, no guessed emails, no changes to sending approval, suppression, privacy or regional gates.
- Pool goal 120; each pass is bounded by search/page/model quotas; no claim of guaranteed yield.
- Preserve existing user profile and settings; keep both live campaigns in REVIEW.

## Review Focus
- Duplicate-only results must advance query rotation across process restarts.
- No-email and blocked-source companies must remain visible without becoming sendable contacts.
- Hiring/legal addresses must not be silently selected for commercial contact.
- Relationship policy must not weaken strict or send-time grounding controls.
- Feedback, draft actions and retries must respect suppression and concurrent jobs.

### Task 1: Persistent diversified discovery and company research
Files: research.py, new discovery.py, db.py, tests/test_discovery.py and tests/test_research.py.
Interfaces: research.discover compatibility wrapper; LeadResearcher.research_company(url) -> Seed; persistent candidate/query records consumed by snapshot.
- [x] Add failing tests for rotation, cooldown, missing email retention, linked-page research and contact selection.
- [x] Implement query rotation, bounded attempts and source/feedback statistics; follow actual same-site links and retain evidence after finding email.
- [x] Run targeted tests and confirm pass.

### Task 2: Referral qualification and review feedback
Files: models.py, service.py, providers.py, web_api.py, tests/test_discovery.py and tests/test_web.py.
Interfaces: campaign prospecting_goal and relationship policy; NEEDS_RESEARCH state; feedback endpoint and explicit draft preparation.
- [x] Add failing tests for imperfect fit retention, strict compatibility, feedback and no sends.
- [x] Implement relationship policy, separate confidence/contact information, feedback/retry actions with existing task service.
- [x] Run targeted tests and confirm pass.

### Task 3: UI, live configuration and verification
Files: static/app.js, defaults/*.json, README.md, tests/browser_scenarios.cjs.
- [x] Add browser assertions for pool progress, retained candidates and feedback actions.
- [x] Implement pool goal, research candidate listing, separated assessment, operator feedback and explanatory controls.
- [x] Pause worker, back up live configuration, migrate the two campaigns and prior confidence rejects without deleting evidence, restart server/worker.
- [x] Run full Python/browser/lint suites, bounded live discovery and independent review; fix important findings and document measured results.

Ruling: no Git repository exists; edit the authorized shared workspace and retain a ledger instead of using Git worktree/commit scripts.
Ruling: execute directly as requested; no extra approval handoff for reversible implementation.
