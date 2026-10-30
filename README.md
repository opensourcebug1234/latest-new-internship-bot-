# p95.ai Reddit lead monitor

A local Python/Streamlit application for finding public Reddit posts and comments, scoring buyer intent, and keeping a human review inbox. It does not send replies, DMs, alerts, or other outbound messages.

## Quick start

Use Python **3.12+**. On this Mac, Python 3.12 is available at `/opt/homebrew/bin/python3.12`; the system `python3` is too old for the pinned environment.

```bash
cd "/Users/animeshjha/internship bot"
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run_pipeline.py --dry-run
python -m pytest -q
streamlit run dashboard.py
```

Open http://localhost:8501. Demo workspace is selected initially. The first dry-run stores eight fictional items; repeating it scores zero existing URLs. Demo runs use deterministic fixture labels, **not an LLM**. They make no API calls, require no keys, and write `demo_leads.db`. Live runs use `leads.db`. Both are ignored by Git.

## Credentials and live setup

Your existing **`apify.env` is supported**. Configuration loads `.env`, then `apify.env`; existing process environment variables win and `.env` wins over `apify.env`. Do not paste keys into chat or commit them. `.gitignore` excludes both files, all `*.env` files, databases, and the virtual environment. `.env.example` can safely be committed.

```dotenv
APIFY_TOKEN=your_apify_token
# Choose one scoring provider:
OPENAI_API_KEY=your_openai_key
# ANTHROPIC_API_KEY=your_anthropic_key
# LLM_PROVIDER=anthropic
# LLM_MODEL=your_model_id
```

The saved Apify token was authenticated using a read-only account request during setup. No paid actor run was made. No LLM key was configured during setup.

Before enabling live scans, edit `config.yaml`:

1. Flesh out `company.description` with p95.ai's actual capabilities. The current category, AI agents and automation, and competitor `resolve.ai` came from your description. No specific product advantages are assumed.
2. Fill `company.ideal_customer`. Add target `subreddits` without `r/` (or with `r/`, both work). Queries can run without subreddits, but configure both to use both discovery channels. Current queries and pain phrases are draft starter searches.
3. Review `queries`, `relevance_terms`, `company.competitors`, and `company.pain_phrases`. The cheap relevance filter uses case-insensitive phrase matches over post title and body. Broader terms improve recall but increase comment-scrape spend.
4. Choose `llm.provider` (`openai` or `anthropic`) and models, or override with `LLM_PROVIDER` and `LLM_MODEL`. The latter overrides only the bulk model. Add its current input/output prices per million tokens to `llm.prices`. Prices start null so the application cannot silently assume a dollar cost.
5. Review the proposed limits: 50 items per actor run, 10 actor runs per scan, $0.25 `max_total_charge_usd` per actor run, 100 scored items per scan, and $1 estimated LLM budget. These are **not authorization to spend**. Limits include both discovery and comment runs. A low run limit can leave later threads unvisited; the run is marked `limited`.
6. Once you approve your settings and spend, set `safety.live_scans_enabled: true`. Then run:

```bash
python run_pipeline.py
# Override the item limit per Apify run:
python run_pipeline.py --max-items 20
# Use an alternate configuration:
python run_pipeline.py --config /absolute/path/config.yaml
```

The sidebar lists missing setup fields and disables the live scan button until they are supplied. CLI preflight fails before making paid requests. Switching off Demo workspace shows the real inbox. The provider is never silently replaced with mock scoring during a live run.

## Scraping and field verification

Read-only setup check (uses your token but starts no actor or LLM requests):

```bash
python run_pipeline.py --check-apify
```

1. Fetch the current default build from `GET https://api.apify.com/v2/acts/trudax~reddit-scraper-lite/builds/default`.
2. Parse its `inputSchema`, validate all run shapes, and pin each run to that build number. Missing or changed required mappings fail before launching runs.
3. Run each keyword independently with `searches`, `sort: new`, a matching Reddit time window, and exact `postDateLimit`. Separately visit each configured subreddit's `/new/` URL. Stage 1 skips comments, users, and community metadata.
4. Normalize URLs and apply age and cheap relevance filters to posts.
5. Visit each relevant thread once per scan with `skipComments: false`, `maxComments`, and `commentDateLimit`. Map comments with their parent post title/body and date before scoring. Existing posts are revisited for new comments; existing comment URLs are never re-scored.

The real schema from **build 5.8.7**, fetched October 30, 2026, is saved in `reference/apify-input-schema.json` for unit tests and reference only. Live execution always fetches the schema again. Confirmed inputs include `searches`, `startUrls`, `sort`, `time`, `maxItems`, `maxComments`, `postDateLimit`, and `commentDateLimit`.

Output mapping follows the actor's published examples: `url`, `username`, `communityName`/`parsedCommunityName`, `body`, `title`, `createdAt`, and `dataType`. Alternative Reddit fields `permalink`, `author`, `subreddit`, `selftext`, and `created_utc` are supported. Canonical URLs discard title slugs, tracking parameters, and host variations while preserving post and comment IDs.

**Still requires a live check:** on your first approved scan, inspect `First post result fields:` and `First comment result fields:` printed in the CLI or dashboard. Only field names are printed. No live result was fetched during development. If output changes, update `lead_monitor/mapping.py` and its tests. Empty/deleted items, user/community records, non-Reddit URLs, malformed dates, and comments without parent context are skipped. Discovery results with no usable posts produce a visible mapping error.

Sources: [actor and output examples](https://apify.com/trudax/reddit-scraper-lite), [Apify Python client](https://docs.apify.com/api/client/python/reference/class/ActorClient), [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs), [Anthropic tool schemas](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools).

## Scoring and caching

`data/few_shots.json` contains eight editable positive/negative examples covering tool requests, competitor pain, operational pain, exploration, news, self-promotion, unrelated posts, and vague agreement. They are illustrative examples, not claims about real Reddit users. Both providers return the same validated JSON schema. A malformed output is retried once; both attempts count toward cost. API transport errors are visible, with no automatic paid LLM retry. Read-only Apify calls have bounded retries/backoff. An ambiguous actor-start failure is **not** automatically retried because it might create duplicate paid runs; check Apify Console first.

Scoring includes company context, current UTC time, age window, examples, parent title/body, and comment text. Text is truncated using `llm.max_text_chars`. Reddit content is treated as untrusted data. Deterministic caps prevent stale threads, bots, discussion-only, unrelated, or self-promotion items from remaining strong leads.

Set `llm.second_pass: true` to refine only bulk scores 4–5 using `llm.strong_models`. Configure prices for that model too. The bulk result is cached before refinement, so a failed second pass can resume without paying again for the bulk pass. Successfully stored leads remain cached even if their text or your prompt later changes. There is no automatic re-scoring or destructive reset command.

A per-database lock prevents concurrent dashboard/cron scans from double-scoring. SQLite uses WAL mode; review edits remain available during scans. A process crash after a remote response but before its cache write can still incur a repeated charge on rerun; no local cache can make remote billing and a SQLite commit atomic.

## Dashboard

- Workspace-wide KPIs: scored items, strong leads, strong new leads to review, replied, converted, and cost this UTC month.
- Sidebar filters: score, status, subreddit, intent, creation date, and case-insensitive literal text search.
- Lead inbox: top 10 strong new leads, parent context, qualification reason, helpful reply angle, and real thread links in live mode.
- Edit `status` and `notes`, then click **Save status and notes**. Statuses: new, replied, responded, converted, ignored.
- Insights: strong leads by creation day, subreddits, competitors, intent types, and query performance. Query metrics count unique **scored** items; a lead found by several queries contributes to each. `query` on the lead preserves its first source; the attribution table keeps all sources.
- Export the currently filtered leads to CSV. Cells beginning with formula characters are escaped for safer spreadsheet import.
- Scan history includes partial/failure states, error details, counts, estimates, and Apify run IDs. Errors also appear while scanning.

## Cost accounting and limits

`runs` stores estimated Apify and LLM costs, including failed/partial runs. Actual Apify `usageTotalUsd` is used when returned. If a charge cannot be confirmed, retain the configured per-run reserve. LLM estimates use returned token usage and your configured prices; a failed request retains a conservative reservation. Retries and the optional refinement pass are included. Check provider invoices for final charges, discounts, and any account-level fees.

Apify `max_total_charge_usd` applies to pay-per-event actors; `max_items` is also sent for pay-per-result limits. These are provider controls, not a universal account spending guarantee. The application never automatically removes a cost limit if the actor rejects it. It limits the number of actor starts, execution timeout, scoring items, and estimated LLM spend. A conservative UTF-8-byte token reservation may stop a scan before its budget is exhausted.

Interrupted runs can remain `running` in history; check the local process and Apify Console before retrying. Actor timeouts limit work if the local process disappears. Errors are redacted before display or storage.

## Scheduling

After live setup and approval, cron can invoke the CLI. Use absolute paths; a scan lock skips overlapping scans. Example every 30 minutes:

```cron
*/30 * * * * cd "/Users/animeshjha/internship bot" && "/Users/animeshjha/internship bot/.venv/bin/python" run_pipeline.py >> /tmp/p95-lead-monitor.log 2>&1
```

This is an example only; **no schedule has been installed**. Repeated scans incur repeated scraping charges even when scoring is cached. On Windows Task Scheduler, set the program to your virtual environment's `Scripts\python.exe`, arguments to `run_pipeline.py`, and “Start in” to the project directory. Choose a recurrence consistent with your approved budget.

## Tests and operational boundaries

```bash
python -m pytest -q
```

Tests mock paid APIs and cover URL dedupe, fields/context mapping, placeholder skipping, JSON validation/retry, prompt construction, scoring caps, actor SDK arguments/schema drift, two-stage attribution, cached comments, budget enforcement, live preflight, status persistence, and Streamlit rendering/scan controls. Test fixtures never contact real Reddit users.

Only public post/comment data is collected. The only stored identity is the public Reddit username; no real-name, email, social-profile, private-data, or de-anonymization enrichment exists. Do not use this tool to target sensitive personal situations. Review [Reddit's terms](https://www.redditinc.com/policies/user-agreement), the applicable data/API terms, and each subreddit's self-promotion rules. Disclose your p95.ai affiliation when replying. Reply only when genuinely helpful, and avoid spam. This tool does not auto-post, auto-comment, or auto-DM.

The dashboard binds to localhost and has no user authentication. Keep it local; do not expose it publicly without adding appropriate access controls. Optional Slack alerts are not implemented or enabled.
