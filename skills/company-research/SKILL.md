---
name: company-research
description: "Gather verifiable public company evidence for Pipeline outreach."
---

# Company Research

Use `pipeline research HTTPS_COMPANY_PAGE` and save its JSON output. The adapter checks public destinations and robots rules, caches briefly, extracts only published email addresses and validates exact research quotes. Inspect the source before using it. Completion requires source URL, quote, source text and timestamp. If a page is unavailable or disallowed, use a permitted source or report the gap.

Run from the Pipeline repository with its virtual environment and the intended PIPELINE_STATE. Core workflows run independently of OpenCode. See [operator setup](../../README.md) for provider configuration and live-action gates.
