# Pipeline

> Local-first outreach pipeline: sourced research, review gates, Gmail sending, reply-aware follow-ups, and CRM tracking.

A local browser app, Python CLI, and persistent worker for a solo AI engineer's customer pipeline. It researches sourced prospects, queues grounded outreach for review, sends approved mail through Gmail, stops follow-ups on replies and tracks conversations through project outcomes. Two independent campaigns ship in `config/`.

**Default mode is REVIEW. No live sending or autonomous campaign was activated during implementation.** This is a working single-host MVP with mocked provider verification; read [scope and limitations](docs/decisions.md) before activating it.

## Open the app

```sh
uv sync --frozen --extra dev
source .venv/bin/activate
pipeline serve --open
```

Open **http://127.0.0.1:8765**. The app initializes the two default campaigns if needed. Use **Setup & costs** to choose/test a model, save your sender identity and voice, connect your Google account for Gmail/Calendar, and set daily usage limits. No environment-file editing is needed for ordinary setup.

The UI includes Overview, Prospects, Review inbox, Sent emails, Conversations, Campaigns, Meetings & projects, Activity, and Setup & costs. Ready emails and Approved / queued filters are available in Prospects and Review inbox. Sent emails provides searchable, paginated history across all campaigns with read-only recipient, subject, body and send-time details. The latest send status appears throughout the app, distinguishing queued, sending, Gmail-confirmed successful sends and sends needing attention. It refreshes automatically and survives reloads. Source research and editor notes sit beside emails. **Edit email** opens subject and body fields from the review inbox or prospect details. Save changes preserves your text without AI rewriting, clears any previous approval, and returns the email to review. Cancel leaves the saved draft unchanged. Keep the opt-out sentence in the body. Edits require fresh approval even in autonomous campaigns; emails already sending or sent cannot be edited. Campaign changes invalidate approvals. Approve & send email queues the approved message and starts the worker immediately. Reapproval clears obsolete approval errors and reschedules parked jobs, while preserving message due times and Gmail cooldowns. **Approve & send all** in Prospects or Review inbox confirms US location for the batch, approves all prepared emails across campaigns, and starts the worker automatically. Sending stays within account/campaign/domain limits; excess emails remain queued for later. Stale, suppressed or held emails are skipped with reasons. Gmail rate limits retry automatically with exponential backoff, jitter and any Retry-After delay, with a shared cooldown that survives restart. Ambiguous send responses stay on hold for reconciliation to avoid duplicates. Start/stop that worker from Setup or Activity, and pause sends from the top bar. Closing the browser tab leaves the server/worker running; keep the computer awake. Stopping the server requests a graceful stop of its own worker.

The server binds to loopback only and uses a local session, same-origin/Host checks, CSRF protection, a content security policy, and masked secrets. It is a single-user local app, not a publicly exposed service. Existing environment variables still override saved settings and are named in setup. Model keys and OAuth files are restricted local files, not encrypted vault entries.

### Lowest-cost setup

| Option | Model bill | What to choose in setup |
| --- | --- | --- |
| Existing computer + Ollama | No model API fee; power/hardware still apply | Local Ollama, a downloaded open model, thinking off initially |
| Gemini AI Studio | Free quota where available, or paid tokens covered by eligible credits | Gemini, the key from the project with your credits, Flash-Lite to start |
| Exa search or company URL imports | No search subscription | Keyless Exa has free rate limits; automatic discovery off by default |
| Hosting | No hosting subscription on your existing computer | Keep the local app running; a sleeping/offline machine cannot process jobs |

On this machine, Ollama and `qwen3.5:9b` were already installed and RAM was 16 GB. A synthetic local qualification passed in about 21 seconds with thinking disabled; the first thinking-enabled attempt timed out. The setup UI can list installed models and, after your explicit download confirmation, prepare Qwen 4B/9B weights with a 16K-context Pipeline profile. Use the smaller model if memory/latency is limiting. The Ollama application itself must be installed and running; downloading weights can use several GB. The local preset rejects known cloud-proxy model names and never silently switches to a paid provider.

For your AI Studio credits, select Gemini and paste the API key from that exact Google project. As checked October 17, 2026, [Gemini 3.8 Flash](https://ai.google.dev/gemini-api/docs/pricing) lists a free tier and paid text rates of **$0.10 per million input tokens / $0.40 per million output tokens**. For illustration, one million input and 100,000 output tokens at those rates is **$0.14**, before other fees/features. This is an example, not a prediction of campaign spend. Availability, quotas, credit eligibility and terms are controlled by Google. A consumer Gemini subscription is not automatically API credit.

The app tracks provider-reported token usage, reserves every attempted model call against a persistent daily cap, and shows optional estimates using prices you enter. It cannot read Google credit balances or guarantee a hard dollar ceiling. Set project limits in AI Studio too. Research is cached and models reuse verified quotations instead of repeatedly receiving whole source pages. An imported prospect normally takes four model calls (qualification, writing, editing, grounding); company-URL research adds one. Retries and responses add calls. Budget estimates do not include unreported thinking, failed-request charges, search fees, power, taxes, or costs entered elsewhere.

Google's [free-tier data-use terms](https://ai.google.dev/gemini-api/docs/pricing) differ from paid usage. Cloud processing of incoming correspondence is a separate explicit setup choice; leave it off to keep that material local, or enable it only if your provider/account terms are appropriate. No automatic local-to-cloud fallback occurs. A connection test uses fictional data and proves JSON compatibility, not full outreach quality. Review actual drafts before enabling autonomy.

Google Cloud project/client creation, account consent, AI Studio key/credit management, and installing the Ollama application still happen in their official account/app flows. The UI links those steps, accepts the resulting client file/key, and handles the rest. The browser cannot grant those permissions on your behalf.

See [frontend verification](docs/frontend-verification.md) for test coverage, screenshots, real-service limits, and browser QA commands.

## Start locally

Python 3.12 recommended (supported range 3.11–3.13), uv, and the existing no-ai-slop skill are needed.

```sh
uv sync --frozen --extra dev
source .venv/bin/activate
pipeline init --config-dir config
pipeline doctor
pytest -q
python examples/dry_run.py --output docs/dry-run-report.json
```

The offline example uses fixture providers and synthetic businesses. It invokes the actual installed editor instruction files, exercises dry-run queuing, switches an isolated fixture database to reviewed mock delivery, ingests a reply, cancels follow-ups, books a mock meeting and records an approved scoped project. It creates no real email or calendar event. [Saved report](docs/dry-run-report.json).

## Configure providers and your identity

Copy `.env.example` to an ignored `.env`, fill in values, then load it in your shell (`set -a; source .env; set +a`). The app does not load dotenv files itself. Use absolute PIPELINE_STATE and token/skill paths, particularly for a service.

You can configure these entirely in the UI. For environment-based deployment, set an OpenAI-compatible JSON chat-completions endpoint and API key (no key for local Ollama), plus model names for RESEARCH, QUALIFY, CLASSIFY, WRITE, EDIT, GROUND, PROPOSAL. Choose a cheaper classification model and stronger grounding/research models when needed. Providers must support JSON-object responses. The model interface is `complete(task, payload) -> dict`; policies, transitions, approvals and suppression stay in deterministic Python. Add actual writing samples to campaign `voice_examples` and truthful credentials to `approved_facts`. No sender achievements or case studies are prefilled.

Automatic search defaults to free, keyless Exa MCP with rate limits. Choose optional Brave in Setup to use BRAVE_SEARCH_API_KEY or a saved key instead. There is no automatic paid fallback. Alternatively research a known public company page or import sourced JSON; search is optional. A page with no visibly published email is skipped; the system never guesses addresses. Discovery does not establish recipient geography or legal basis.

Edit each campaign JSON with your real sender name, Gmail address, postal address, bounded offer and relevant keywords. Then:

```sh
pipeline configure config/ai_automation.json
pipeline configure config/ai_engineering.json
```

Changing any campaign field invalidates existing autonomous authorization and draft approval. Run `pipeline redraft CONTACT_ID` for an unsent initial draft under the new configuration before sending; do not patch stored message content. Add any new campaign JSON and run `pipeline configure path/to/campaign.json`; no code change is required.

### Actual no-ai-slop dependency

The runtime reads `NO_AI_SLOP_DIR/SKILL.md` and `eval.md`, defaulting to `~/.agents/skills/no-ai-slop`. Both were found in this environment. The live editor receives the complete actual skill and eval as system instructions; it must return the edited draft, What changed and evaluation outcome. The application stores their combined hash, edit audit and grounding result with every message. Missing files or a failed edit stop the message.

On another host, copy your existing installed `no-ai-slop` directory (including eval.md) to that host, or install the same trusted skill source through your agent's skill installer, then set NO_AI_SLOP_DIR. This project does not substitute a regex approximation. The offline test model only checks that these actual files reach the editor stage; live writing quality must be reviewed with your chosen model.

### Google OAuth

In **Setup & costs → Google account**, click **Connect Google**, choose your account, approve the requested access, and return to Pipeline. Reconnect from the same card when needed.

A one-time registration is required for a standalone application: create a Google Cloud OAuth app, enable Gmail API and Calendar API, add your account as a test user if the app is in Testing, and create an **Installed/Desktop app** OAuth client. Open **One-time app registration & connection settings** to upload its JSON or paste the desktop client JSON directly. The client secret is saved locally and never echoed in settings responses. The command-line alternative is:

```sh
pipeline auth --client-file /secure/path/google-client.json
```

The browser consent requests Gmail read-only and compose (draft/send), Calendar event access and free/busy. The token is written to PIPELINE_GOOGLE_TOKEN or state/google-token.json, mode 600. Keep its directory mode 700, outside shared backups/source control. OAuth tokens are not encrypted by this application; use OS/disk encryption and a secret store on the host. Move the token securely to the persistent host if authorizing elsewhere. Testing-mode OAuth refresh tokens may expire; configure the Google project appropriately for unattended operation. Do not reuse an unrelated personal mailbox: a dedicated consulting mailbox limits incidental metadata exposure.

Use the authenticated Gmail address as sender. Configure sending-domain SPF/DKIM/DMARC with your mail provider. Check Google consent/scope and sender requirements in the [source notes](docs/decisions.md). `pipeline doctor` reports presence of credentials, not token validity or provider acceptance.

## First real prospect, reviewed

```sh
pipeline research https://a-company.example/contact > prospect.json
pipeline prepare ai_automation prospect.json
pipeline inspect 1
pipeline contact-review 1 --region US \
  --legal-basis 'US commercial outreach; business relevance reviewed' \
  --provenance 'Official company contact page; source terms reviewed'
pipeline queue
```

The `.example` URL is a placeholder: use an actual company page you may retrieve. `research` and `prepare` call the configured LLM. Seed files contain company/domain, published email, source URL, research quotes/full source text with fetched timestamps, and region/provenance/verification fields. See [example input](examples/prospect.json). Imported source assertions are operator-supplied; inspect them. `verification=published` means present on a source page, not guaranteed deliverable. US location confirmation, a published or verified address, and a sender name and email are mandatory before sending. Business relevance and source notes are optional. Postal address is optional in the app; US commercial email generally still legally requires a valid postal address, including manually reviewed outreach. Unknown and non-US locations are blocked.

`pipeline discover ai_automation` runs a bounded live search/research pass. `pipeline schedule-discovery ai_automation` places it on the durable outbox. A running worker performs one bounded discovery pass per campaign per day only when automatic discovery is enabled in Setup & costs. It is off by default. Each company receives at most one treatment/contact in this MVP, including across campaigns; opt-outs also suppress the company. Inspect/review contacts before approving mail.

Only after reviewing a message and its source evidence:

```sh
pipeline approve MESSAGE_ID_1 MESSAGE_ID_2
```

This **authorizes and enqueues live sending**. A running worker sends due approved messages within limits. Without a worker, `pipeline send MESSAGE_ID` performs the same gated operation immediately. Never approve merely to test a code path. Use DRY_RUN campaigns or the offline example for tests.

## Run unattended

```sh
huey_consumer pipeline.worker.huey -w 1 -k thread
```

Keep the environment and PIPELINE_STATE identical between worker and CLI. Use one persistent local-disk host and one consumer. Gmail polling stays off until an outreach send attempt (SENT, SENDING, or UNKNOWN). Afterward, incremental polling may inspect unrelated message headers to match replies; bodies are retrieved only for tracked threads or matching outbound message references. Unmatched delivery reports require manual inspection. Gmail is then polled each minute; reply events persist before classification and immediately cancel pending initial/follow-up sends. Follow-ups default to 4 business days, then 7 additional business days; the second is the final one. They pass through the same editor, grounding and send gates. No reply, takeover, opt-out, hard bounce or meeting booking automatically restarts a sequence. Business days exclude weekends; regional holidays are not implemented.

Review mode requires approval for each follow-up. Low-confidence replies, interested leads and questions appear in escalations. `pipeline respond CONTACT_ID` drafts an answer using approved facts; `--category scheduling` includes fresh Calendar slots. Default autonomous categories are initial/followup only, so these replies remain in review. Scope, pricing, contracts and work commitments require explicit review. Inbox content never changes campaign policy.

For a managed Linux service, use [deployment instructions](deploy/README.md). No host was provisioned or service installed by this work.

## Operator commands

```sh
pipeline status
pipeline inspect CONTACT_ID
pipeline conversations
pipeline queue
pipeline followups
pipeline meetings
pipeline escalations
pipeline failures
pipeline replay DEAD_JOB_ID
pipeline cancel JOB_ID
pipeline reconcile UNKNOWN_MESSAGE_ID
pipeline takeover CONTACT_ID
pipeline suppress person@example.org --reason 'Requested no outreach'
pipeline digest
pipeline metrics ai_automation
pipeline optimize
```

A missing sent-mail search result after an ambiguous send **never** permits automatic resend. Reconcile with Gmail and inspect the original mailbox manually. `replay` retries a dead/cancelled workflow, preserving send reservations; it cannot bypass an UNKNOWN message. The daily digest is stored locally; `pipeline digest` computes a current report. No digest email or other unsolicited notification is sent.

### Meetings and business outcomes

```sh
pipeline slots ai_automation --start 2026-11-02T09:00:00-08:00
pipeline book CONTACT_ID --start 2026-11-02T10:00:00-08:00 --approve-invitation
pipeline meeting-completed CONTACT_ID
pipeline proposal CONTACT_ID
pipeline win CONTACT_ID --free --approve-scope
pipeline complete PROJECT_ID --hours 2 --revenue 0 \
  --customer-value 'Customer-reported outcome' --verified-results 'Actual measured result with source'
pipeline record-feedback PROJECT_ID --testimonial 'Customer-approved wording'
pipeline case-study PROJECT_ID
```

Dates are examples; use an agreed future slot with an explicit timezone. Booking sends a real Google invitation, checks fresh free/busy and uses a stable event ID. Natural-language ambiguity is escalated rather than guessed. `win` records a user-confirmed agreement; proposal generation does not send a proposal or sign a contract. Free assessments have configured hour and project-count bounds. Record only verified outcomes. Case-study publication and testimonial/referral outreach require review. Metrics separate replies from positive replies and record per-campaign revenue; costs/time include only what the operator has entered.

## Enable autonomous sending

Do this only after reviewing real campaign drafts and provider behavior. Edit the selected campaign JSON to `"mode": "AUTONOMOUS"`, configure it, inspect the printed config_hash, then explicitly authorize that exact hash:

```sh
pipeline configure config/ai_automation.json
pipeline authorize ai_automation --hash EXACT_CURRENT_CONFIG_HASH
```

This is the explicit live campaign authorization. Changes to configuration revoke it by hash mismatch. Autonomous mode sends only configured permitted categories, within unchanged campaign/domain/account limits, and still checks recipient review, evidence/editor approval, pause and suppression. It never automatically raises limits, changes offer commitments or authorizes new pricing/scope. To return to review, configure the campaign with `"mode": "REVIEW"`.

## Pause or stop

```sh
pipeline pause                  # Global email and invitation pause
pipeline pause ai_automation    # One campaign
pipeline resume ai_automation
pipeline resume                 # Clear global pause
```

Inbox processing continues while paused. Stop the Huey process (Ctrl-C locally, `systemctl stop pipeline` on the documented host) to stop all activities. A request already accepted by a provider cannot be recalled. Restart recovery preserves message reservations and workflow state. Back up and protect the state directory; retain suppression records.

[Design and boundaries](docs/decisions.md) · [Implementation plan](docs/superpowers/plans/2026-10-17-pipeline.md) · [Original requirements](docs/REQUEST.md)

## Reusable agent skills

The thirteen independently scoped definitions in [skills/](skills/) use SKILL.md frontmatter compatible with OpenCode. Install/symlink the desired directories into your OpenCode skill path, or read them in another agent. They route to the same tested CLI and do not embed a second scheduler or bypass live-action gates. The standalone worker does not require these agent sessions to stay open.

See [current model/search choices and business assessment](docs/models-search-and-business.md).

Google Search grounding is available through **Research with Google** in Prospects and Setup & costs. It uses the configured Gemini research model and AI Studio API key through Google's native Interactions API. The answer opens in an isolated, session-protected viewer with citations and Google's search suggestions; the view expires after 15 minutes or a server restart. It is interactive research only: grounded output and links are not fed to automated discovery, prospect records, or email drafts. Google's grounding terms prohibit automated collection of returned links for crawling or database building. Exa remains the free automatic discovery provider. Google research attempts count toward the shared daily model-call cap and token tracking, including reported thinking tokens. Separate search charges are excluded from the UI token estimate. Current Gemini 3+ paid-tier pricing includes 5,000 search queries/month shared across models, then $14 per 1,000 queries, plus model tokens. One request can issue multiple searches; the app does not know your remaining Google allowance. See [Google grounding](https://ai.google.dev/gemini-api/docs/google-search), [pricing](https://ai.google.dev/gemini-api/docs/pricing), and [grounding terms](https://ai.google.dev/gemini-api/terms#grounding-with-google-search).

Use **Prospects → Find prospects** for automatic company and email discovery. Pick a campaign, set the qualified-prospect target, and optionally enable a daily pass. Exa supplies candidate URLs; the configured model extracts sourced business/contact facts, scores fit, and prepares drafts. If the first company page has no email, Pipeline checks `/contact` and `/contact-us` on the same host, up to three pages total, respecting the existing public-source/robots limits. Only visibly published addresses are accepted, and both business evidence and the email source are retained. No email-pattern guessing or deliverability guarantee is provided. Good-fit drafts await approval in REVIEW mode; unknown roles or recipient geography remain unverified. The Prospects page shows discovery counts and failures. Daily passes require the local server/worker and an awake computer; the same worker also processes approved outbound messages. A target is a stopping point, not a promise: no matching published contacts, source restrictions, and budgets can produce fewer results. Each pass checks at most three configured search queries and five URLs per query.

### Building a free-help referral network

Default campaigns automatically prepare review emails at 50% fit or higher and aim for 120 distinct contacts total (60 per campaign) to offer up to two hours of free practical help or teaching with scope agreed first. Referrals are welcome, never required. Choose **Free help, teaching and relationships** in Campaigns to retain imperfect fits; strict paid-fit campaigns remain available. The pool goal is separate from daily sending limits and email approval.

Prospects shows pool progress, actual people reached, evidence confidence, contact suitability, operator feedback, search yield, and companies still missing a usable contact. Discovery follows real services/team/contact links, uses bounded same-company contact searches, rotates locations and angles across restarts, excludes known/currently cooling domains, and reconsiders due companies. Published hiring/legal/privacy inboxes are excluded from new extraction. Click a company or **Review** to screen the evidence and email together. Check the single US confirmation and sending authorization checkbox, then **Approve & queue email** on that screen. Additional notes are optional; the review inbox offers the same inline approval. Default relationship campaigns automatically draft at **50% fit or higher**; change the threshold or disable automatic drafting in **Campaigns → Manage**. **Generate eligible drafts** queues existing eligible prospects without approving any email. Every draft still needs US location confirmation and explicit send approval. Existing unsent drafts get the opt-out wording replaced directly without model calls; any changed approved drafts return to review.

Each pass is bounded by four searches, 25 company attempts and the shared model/page budgets. Each company uses up to six linked/fallback pages and, when needed, up to two targeted-search result pages. Retained companies are not proof of a fit or a working email. Daily model caps remain authoritative; temporary limits schedule a continuation and daily limits resume the next UTC day. Automatic pass spacing is configurable from 1–24 hours in Setup & costs. Free Exa remains the default with no paid fallback; Gemini model usage still consumes your credits or API allowance. A 120-contact goal is not a guarantee of yield, free cloud usage, responses, referrals or revenue.

### Practical throughput and compact prompts

Defaults are 1,000 model calls/day and 60/minute, 50 contacts per discovery pass, up to 12 searches and 100 company checks per pass, 20 searches/minute, 240 page fetches/minute globally and 20 per company. All are adjustable in **Setup & costs → Discovery & limits**. Local limits do not override provider quotas. Raising throughput wakes budget-deferred jobs; minute-level preparation limits retry after a minute. Review mode remains explicit per-email approval. Default approved-send pacing is 25/day per campaign, 50/day across the account, 2/day per company, at least 120 seconds apart.

Model prompts contain task-specific context rather than the entire campaign. Qualification omits the sender CV and operational settings; drafting/grounding receive up to three exact relevant approved facts, exact evidence quotes and the bounded offer. Full pages and compliance metadata stay local. Research uses exact page excerpts around distinct usable addresses and business context. Incoming conversation context still requires cloud-correspondence permission.

The editor uses the actual installed no-ai-slop rule names, word rules and complete evaluation checklist, omitting its interactive handbook/examples. Unknown skill formats fall back to full sources. Audit records retain full source hashes and a separate runtime-profile hash. Writing, editing and final factual grounding remain separate calls. Short tasks have small response defaults; each task can be overridden in Setup, under the global response ceiling. Truncated responses get at most two larger retries, each charged against the same local call budget.
