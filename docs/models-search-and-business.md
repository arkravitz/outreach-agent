# Model/search update and business assessment — October 18, 2026

Gemini defaults to `gemini-3.8-flash`. Setup offers a profile that routes `write` and `edit` to `gemini-4-argon`, leaving routine tasks on Flash. Current Gemini models use low thinking for short structured tasks. Catalog listing uses Google's native `/v1beta/models`, header authentication, pagination, and generation-capability filtering. Suggested IDs are not a promise that a project has access. Missing keys, denied access, and rate limits produce actionable errors without echoing credentials or provider error bodies. The inspected saved Gemini configuration had no key; authenticated Google testing could not be completed.

Free keyless Exa MCP search is the default search provider. Brave is optional and explicitly selected; there is no automatic paid fallback. Daily discovery remains opt-in and bounded to three queries per campaign, five results per query, with existing domain deduplication, public-source retrieval policies, and manual contact review. Search queries go to Exa. Keyless usage has rate limits and no availability guarantee. A limit pauses that attempt; known company URLs remain an alternative. Search results supply candidate URLs, not verified contact facts.

Current standard introductory Gemini prices per million input/output tokens are Flash $0.75/$3.75 through December 31, 2026 ($1.50/$7.50 afterward), and Argon $2/$10 through November 30 ($4/$20 afterward). Argon has no free tier. For an illustrative 10,000 input plus 2,000 billed output tokens, one request costs $0.015 on Flash or $0.04 on Argon at introductory rates. Thinking counts toward billed output. These are request examples, not whole-prospect costs: research, qualification, writing, editing, grounding and retries add calls. The UI's optional single price pair cannot accurately price mixed-model routing; provider billing is authoritative.

The business makes sense as a way to reduce administration around a focused consulting offer. Its hardest assumptions are customer need, trust, reachable decision makers, and willingness to pay. Public evidence cannot establish internal pain or budget. A fluent email or high model score is not proof of either.

Start with one narrow niche and one bounded outcome, such as document intake for one kind of service firm. Review the first 20–30 prospects and messages yourself, at about five new prospects daily. Judge qualified replies, attended meetings, paid pilots, completed projects and revenue. If conversations fail to materialize, revise targeting/offer before adding volume. Keep free assessments bounded; offer a priced pilot once a real problem and buyer interest are confirmed. Case studies must come from actual delivered results. The existing broad campaigns are starting templates, not validated market segments.

Sources checked:
- https://ai.google.dev/gemini-api/docs/pricing
- https://ai.google.dev/gemini-api/docs/latest-model
- https://ai.google.dev/gemini-api/docs/openai
- https://ai.google.dev/api/models
- https://exa.ai/docs/get-started/exa-mcp
- https://docs.opencode.ai/docs/tools/
