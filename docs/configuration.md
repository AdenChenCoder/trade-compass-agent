# Configuration

Trade Compass keeps installed-application state separate from package files.

```text
~/.trade-compass/
├── config.yaml
├── .env
├── data/
├── memory_vault/
├── backups/
└── mcp.json                 # optional
```

An installed application uses this layout. Run `trade-compass setup` to create
it and complete the guided configuration; run `trade-compass configure` to
revisit the same wizard later.
Existing secrets are retained when their masked prompts are left blank, and
changed files receive a neighboring `*.setup.bak` recovery copy.
The TUI uses `↑/↓` for navigation, `Space` for multi-select, and `Enter` to
confirm the current item.

In a source checkout, `config/default.yaml`, `.env`, `data/`, and
`memory_vault/` remain the development defaults. Setup only initializes those
files unless `--wizard` is passed explicitly.

## Installed package paths

`uv tool` chooses platform-specific package directories. Inspect them instead
of assuming a hard-coded `~/.local` path:

```bash
uv tool list --show-paths
uv tool dir
uv tool dir --bin
```

- `uv tool dir --bin` contains the `trade-compass` command.
- `uv tool dir` contains the isolated package environment; the application does
  not store writable user state there.
- `TRADE_COMPASS_HOME` defaults to `~/.trade-compass` and contains writable
  application state.
- `config.yaml` stores non-secret settings; `.env` stores credentials with
  owner-only permissions.
- `data/` and `memory_vault/` are the default data and long-lived memory roots.
- `backups/` contains recovery archives; optional MCP configuration is
  `mcp.json`.

Set a custom application root before the first setup:

```bash
export TRADE_COMPASS_HOME=/path/to/trade-compass
trade-compass setup
```

The same environment variable must be present when running the command or
installing a persistent service.

On macOS, `trade-compass service install` writes
`~/Library/LaunchAgents/com.trade-compass.serve.plist`; logs default to
`~/.trade-compass/data/logs/serve.stdout.log` and `serve.stderr.log`. On Linux,
the user unit is `$XDG_CONFIG_HOME/systemd/user/trade-compass.service` when
`XDG_CONFIG_HOME` is set, otherwise
`~/.config/systemd/user/trade-compass.service`; view logs with
`journalctl --user -u trade-compass.service -f`.

## Resolution order

The important location overrides are:

| Variable | Purpose |
| --- | --- |
| `TRADE_COMPASS_HOME` | Installed-application state root |
| `TRADE_COMPASS_CONFIG` | Explicit YAML configuration file |
| `TRADE_COMPASS_ENV_FILE` | Explicit dotenv file |
| `TRADE_COMPASS_DATA_DIR` | Runtime market and operational data |
| `TRADE_COMPASS_MEMORY_DIR` | Long-lived memory and user-created Skills |
| `TRADE_COMPASS_DATA_PROVIDER` | Selected market-data provider |
| `TRADE_COMPASS_PORT` | Web/API port |

Environment variables override their corresponding YAML values.

## LLM providers

### Switching connections

Select a local model or API provider in Settings → Agent connection, or run
`trade-compass setup`. Connection checks report configuration status; a model
request confirms availability. Changes apply to subsequent tasks.

Saving connection settings retains the first previous configuration beside
the active YAML as `*.llm-backup.yaml`. To recover, stop new tasks, restore that
file to the active config path, and restart Trade Compass. History and memory
are not migrated or removed by a connection change.
Previously selected models and credential environment-variable names are retained per provider under `llm_connections`, so switching back can
reuse them. Secret values continue to live in the existing credential stores.

### API and local model providers

The `llm` section selects a provider, model, API-key environment variable,
timeout, and retry count. Supported adapters include DeepSeek, OpenAI,
Anthropic, OpenRouter, DashScope, Ollama, and LM Studio.

The source and installed defaults use LM Studio's OpenAI-compatible server at
`http://localhost:1234/v1` with `qwen3.8-27b` for chat and image input.
Start the LM Studio local server and load the model with a 128K context before
using the agent, for example:

```bash
lms server start
lms load qwen3.8-27b --context-length 131072 --identifier qwen3.8-27b
```

Keep the model loaded while using Trade Compass. Local providers do not need API
keys; the setup wizard defaults to this LM Studio model as well. The Ollama
provider remains available as an optional alternative.

The 128K window is shared by input, reasoning, and the final answer. Defaults
allocate at most 98304 input tokens (96K), including instructions and tool
schemas, and reserve 32768 output tokens (32K). The input budget is also capped
by the window actually loaded in LM Studio, rather than the model's advertised
maximum. Token counts are estimates; financial digits and ASCII punctuation
count individually to avoid treating numeric records as cheap prose. The adapter checks each request with a
conservative margin and stops an oversized request without replaying actions.

Context compression retains at least the most recent 20 messages or about 64K
tokens of recent source material, whichever protects more history. Older tool
results can be shortened; conversation summarization begins at 80% of the input
budget (about 76.8K tokens with these defaults). Full session history remains
available. Changing compression settings takes effect on the next Agent turn.

LM Studio requests stream progress and do not automatically replay timed-out
generations, even when `llm.max_retries` is nonzero. The streamed request read
timeout defaults to 900 seconds without incoming data; this is separate from
the task's total time budget. Scheduled Agent steps and custom prompt jobs have a 30-minute
budget; the main research workflows have a shared 60-minute budget. Postmarket
archive and weekly review each have a 180-minute budget for full historical
knowledge review and Dreaming. The intraday autonomous trading workflow has a 30-minute outer budget,
including a 29-minute Agent step, and retains its existing overlap and trade
execution guards. Nested work always respects the earlier parent deadline.
These longer task budgets apply regardless of the selected model. Explicit
caller deadlines still take precedence. `llm.agent_timeout` defaults to 3600
seconds. Direct memory model calls,
Dreaming's procedure extraction and daily/weekly diary sessions also use this budget, while
remaining bounded by the earlier workflow deadline.

Completed batches of deferred knowledge reviews are reused while candidate
versions, active knowledge, user rules, evidence and review policy remain the
same. New batches contain at most four candidates; valid completed larger
batches from earlier versions are still reused. A malformed response gets one
bounded repair for unresolved candidates; exact valid judgments are retained.
Identities are never guessed, and any admission or retirement still receives
independent review against the complete original batch and snapshot. Knowledge adopted later in a
round does not mark earlier judgments as already checked against that new core.
Repeated recalls of unchanged Skills are not new market evidence; new
independent market observations still reopen review. These fingerprints are
rebuildable maintenance metadata; a forced review bypasses them and preserves
the existing knowledge history.

For Qwen3.5 and Qwen3.8, factual session and time-tree summaries use the raw
completion endpoint with a closed thinking block and a 4096-token output limit.
This also works with downloaded MLX models that do not expose reasoning controls
through LM Studio's chat API. Knowledge reassessment, evidence validation, pattern
discovery, experience extraction, outcome attribution, and trading decisions keep
normal reasoning through chat completions.

LM Studio Qwen3.8 tool rounds return their reasoning as context for the next
model call in the same turn. This transient context counts toward the input
budget without spending the recent-source protection quota. It is excluded
from saved conversation records and factual summary inputs; the model's default
reasoning effort is retained.

Local HTTP LM Studio requests use the loaded model's native prompt template and
tokenizer to measure input, including tool definitions and transient reasoning.
Counting never loads a model and has a two-second limit, bounded by the earlier
task deadline. Unsupported endpoints or unavailable native counting retain the
conservative estimate. Context checks run between tool rounds as well as at turn
entry. Compaction preserves the current request, recent original sources, tool
call/result pairs, and current-turn loaded Skills and trade receipts; raw
conversation history remains accessible.

LM Studio Qwen3.8 thinking requests use its
[recommended sampling](https://huggingface.co/Qwen/Qwen3.8-27B#best-practices):
temperature 1.0, top-p 0.95, top-k 20, presence penalty 0.0, repeat penalty 1.0.
They allow up to 32768 output tokens, including reasoning, when the loaded window
is at least 64K, and 16384 with a smaller window. Explicit vision output limits
are preserved. Non-thinking factual summaries use temperature 0.7,
top-p 0.8 and presence penalty 1.5. The model's default thinking effort is retained.
Qwen3.5 remains supported with presence penalty 1.5 for thinking and the existing
8192-token scheduled output budget. Other models and providers retain their
sampling settings.

An exhausted output budget is reported as an incomplete result, rather than
publishing reasoning text as the answer. No global LM Studio template change is
required; the same adapter is included in source checkouts and installed wheels.
Cancellation shuts down the active model socket before closing the response,
so a stalled read cannot continue waiting after the shared deadline.
If a local generation ends with reasoning but no answer, the Agent may finalize
once from existing tool results while retaining normal reasoning. This step has
no tools and shares the original deadline; it cannot replay an order. Detected
unverified execution claims fail before report publication or learning.

Rejected simulated orders retain their actual reasons under unfilled orders in
the final report. A rejection such as T+1 is distinct from an unavailable data
provider; quote failures still remain visible as the reason an order did not fill.
Sell receipts expose FIFO realized net profit after allocated entry and exit
fees. The trading Skill requires a portfolio refresh before recording profit
claims or post-trade balances in reports and memory.
Daily journals and weekly strategy reviews use a separate execution session per
run; earlier sessions remain searchable as historical evidence.
Portfolio results distinguish configured account capital from total currently
available cash. An unknown account balance leaves the cash total unknown. Report
corrections receive these account facts and cannot execute tools or reset the
original deadline.

Keep credentials in `.env`; do not place them in `config.yaml`, Skills, issues,
logs, or portable examples.

Installed-app users should normally use the setup/configure wizard instead of
editing either file directly. The files remain documented as an advanced and
automation-compatible storage format.

## Web search

Web search defaults to free multi-engine search. A saved API key alone does not
enable paid search. To select DeepSeek Search, add these settings to the active
`.env` (the repository root in a source checkout, or `~/.trade-compass/.env` for
an installed application, unless `TRADE_COMPASS_ENV_FILE` selects another file):

```dotenv
WEB_SEARCH_PROVIDER=deepseek
DEEPSEEK_API_KEY=your-key
```

To try free search first and use DeepSeek only when all free sources fail:

```dotenv
WEB_SEARCH_PROVIDER=auto
WEB_SEARCH_FALLBACK_PROVIDER=deepseek
DEEPSEEK_API_KEY=your-key
```

The free chain tries Brave, DuckDuckGo, Yahoo and Google within its time budget,
then Google News. A successful news fallback retains `data_scope: news` and does
not trigger a paid request. Only if that entire free chain fails does the tool
make at most one DeepSeek request. Its actual provider remains `deepseek`;
successful results carry `data_status: fallback`, `fallback_from: free`, and the
free failure in `fallback_reason`. DeepSeek errors, rate-limit retry deadlines,
and empty results stay visible alongside that reason. An invalid or overlong
DeepSeek query fails without silently changing it or spending a request.

The free chain has a 12-second budget and DeepSeek has a separate 10-second
caller deadline. The Agent allows enough time for both stages, subject to the
current task's overall deadline. A saved key alone never enables this fallback.
`WEB_SEARCH_FALLBACK_PROVIDER` defaults to blank and currently only accepts
`deepseek` when enabled; it applies to `auto`/`free` search, not directly selected
paid providers. Clear it and restart to disable paid fallback.

Restart the running application or service after editing `.env`. Exported
environment variables take precedence over that file. Search selection is
independent of `llm.provider`, so local models and other agent providers can
use DeepSeek Search too. The integration uses the HTTPS API directly and does
not require an optional SDK or package extra.

The existing `web_search` tool returns source links, short and long summaries,
site names, and publication dates when supplied by DeepSeek. `fetched_at` is the
request completion time, not the article's publication date; unknown publication
dates stay unknown. Supplied dates carry `published_date_verified: false`:
provider dates can differ from dates in the original page, even with date
filters. `rerank_score` and `authority` are provider ranking metadata, not
verified claim confidence. Check original sources before judging recency or
using search results as research evidence. Summaries are bounded to 2,000
characters per result; longer ones carry `summary_truncated: true` and retain
their source URL for further reading. Full-page reading continues to use `fetch_url`;
this integration does not enable DeepSeek Fetch or request full page bodies.

DeepSeek queries must contain 1–128 Unicode characters, including any `site:`,
`lang:`, `after:` or `before:` filters. Longer queries fail visibly instead of
silently losing filters. Result counts are bounded to 1–50. No matches return
`count: 0` and `data_status: empty`; request failures return an `error` and
`data_status: unavailable`. Missing credentials, insufficient balance, rate
limits and timeouts do not trigger further provider changes. Each DeepSeek call
makes one request without automatic retries, with a 10-second caller deadline and bounded
network timeouts. HTTP errors retain the status and any `Retry-After` value.

DeepSeek Search is billed separately from model inference. Review the
[official pricing and limits](https://api-docs.deepseek.com/zh-cn/guides/search/)
before enabling it; reading returned summaries with a cloud model can also
consume model tokens. Search queries are sent to the selected service.

Set `WEB_SEARCH_PROVIDER=auto` (or `free`), clear
`WEB_SEARCH_FALLBACK_PROVIDER`, and restart to use only free search.
Use `WEB_SEARCH_PROVIDER=tavily` with `TAVILY_API_KEY` to select Tavily. Changing
the search source does not migrate or remove history, memory, or research data.

## Optional package features

Chart rendering used by the built-in stock-analysis agents is part of the
default installation. Extras are only needed for additional integrations and
heavier capabilities.

Market data uses free sources by default. With `data_provider: auto`, configure
`TUSHARE_TOKEN` and enable `data.tushare_enabled` to prefer Tushare for supported
daily bars and fundamentals, including bulk screening. A saved token alone does
not enable paid requests. Valid Tushare daily caches can be reused; other cached
sources and free providers become fallbacks when Tushare fails or returns stale
data. Minute bars continue to use the existing free providers because this
integration only supports completed daily bars. Existing caches are retained
when the option is disabled; subsequent network requests use free sources.

Batch requests follow the same preference. Large stock screens fetch Tushare
history by trading date and reuse the existing cache; batch fundamentals use
daily snapshots. Missing or failed symbols fall back individually, and results
retain their actual source and data status. Invalid placeholder values are not
treated as company fundamentals; funds and indices report this data as not
applicable. A successful request alone does not establish data completeness.

These providers use the official HTTPS API directly. The `tushare` extra remains
available for users who also need the vendor SDK; it is not required by the
built-in providers.

Add an extra to an installed tool:

```bash
uv tool install --force --python 3.12 'trade-compass-agent[tushare]'
```

Add the same extra to a source checkout:

```bash
uv sync --extra tushare
```

Feishu bots require the `channels` extra, including the `lark_oapi` SDK.
For source checkouts, include it whenever synchronizing the environment:

```bash
uv sync --extra dev --extra channels
```

For an installed tool, include `channels` alongside any other extras you use,
for example `uv tool install --force --python 3.12 'trade-compass-agent[channels]'`.
Synchronizing a checkout without `--extra channels` can remove these optional
dependencies even while a previously started service is still running.
After changing the service's environment, restart it with
`trade-compass service restart`. A completed analysis does not guarantee message
delivery; each attempted channel delivery is recorded separately in the task's
run details, and failures also produce warning notifications.

Confirm the resulting environment with:

```bash
trade-compass doctor
```

## Source development

Source checkouts use `config/default.yaml`. The packaged defaults are maintained
separately in `config/packaged.yaml`; changes that affect installed users should
consider both files explicitly.
