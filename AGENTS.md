# AGENTS.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## Product contract

Trade Compass Agent is a local-first A-share research and trading workbench
delivered as one Python package with a bundled Web UI.

Preserve these load-bearing contracts unless the task explicitly changes them:

- Source checkouts and installed wheels expose the same user journeys.
- Installed package assets are read-only; writable state belongs under the
  configured data and memory roots.
- Human-owned rules and pinned memory outrank agent-created memory.
- Built-in Skills are versioned package assets; runtime-created Skills remain
  in the writable memory vault.
- Persistent restore/import operations preview by default and preserve a
  recovery path.
- Web, CLI, API, scheduled jobs, and services must not invent separate business
  semantics for the same operation.

## Development and stable runtime

- Source checkouts are for development and isolated validation. The stable,
  long-running investment service must use an installed, versioned package
  published from reviewed code pushed to the remote repository.
- Local builds, editable installs, passing tests, and source service restarts do
  not constitute a production release. Acceptance must include installing the
  published version outside the checkout and exercising its supported entry point.
- Development and validation must use separate configuration, data, memory,
  ports, and process supervision from the stable service. Tests must not execute
  live investment tasks or replay missed trades to establish correctness.
- Before editing or restarting, confirm which executable, configuration, data,
  memory, and assets the stable service actually uses. If a legacy service still
  runs from source, validate in an isolated copy and preserve the existing
  service until the replacement package has passed acceptance and cutover is
  authorized. Never assume running Python has already loaded every source file.
- Cutover and upgrades must preserve account records, sessions, schedules,
  memory, credentials, mobile identity and pairings, and a recoverable backup.
  Only one service may write to the production roots. Retain the previous
  package version and service definition; verify data compatibility before rollback.
- Follow `docs/development-production.md` for release and cutover gates.

## Repository map

| Path | Responsibility |
| --- | --- |
| `src/trade_compass_agent/runtime/` | Agent loop, tools, context, Skills, specialists |
| `src/trade_compass_agent/web/` | FastAPI routes and HTTP contracts |
| `apps/web/` | React workbench |
| `.trade-compass/skills/` | Source for packaged built-in Skills |
| `src/trade_compass_agent/workflows/` | Packaged workflow assets |
| `config/` | Source and installed defaults |
| `schemas/` | Reader and workflow data contracts |
| `scripts/` | CI, security, build, and release verification |
| `tests/` | Unit, integration, and consumer-facing checks |

## Where capabilities belong

- Repeatable instructions and output contracts belong in a Skill.
- Deterministic data access, calculation, or mutation belongs in a runtime tool.
- A focused reasoning role belongs in a specialist asset.
- A scheduled multi-step product flow belongs in a workflow.
- Shell command metadata belongs in `command_catalog.py`; keep compatibility
  aliases when moving commands into resource/action groups.
- UI code may present backend behavior but must not duplicate authoritative
  rules or persistence logic.

## Temporary documents

- Store all temporary project documents under the repository-root `/temp/`
  directory (`<repo>/temp/`), using task-specific subdirectories when needed.
  This includes development draft plans, scratch notes, investigation reports,
  code review reports, and ad hoc validation results.
- Keep `/temp/` ignored by Git. Never stage, commit, or force-add its contents.
- Put only durable project documentation in `docs/`; do not scatter temporary
  documents across the repository root, `docs/`, `data/`, or source directories.
  Application runtime data, including generated reports, execution logs,
  caches, memory, and recovery backups, stays in its configured data and memory
  roots. Classify files by their producer and use, not by directory name,
  filename, age, or file format alone.

## Common commands

```bash
uv sync --extra dev
scripts/ci_check.sh
uv run pytest tests/test_runtime_skills.py tests/test_command_catalog.py
pnpm --dir apps/web test
pnpm --dir apps/web typecheck
pnpm --dir apps/web build
uv build
python scripts/check_dist.py
```

Run installed-wheel checks outside the repository so Python cannot accidentally
import `src/` instead of the built package.

Workflow tests must pass isolated data and memory roots even with `persist=False`;
diagnostics and nested workflows can still write state. Mock model execution in
unit tests explicitly: clearing API keys does not disable a local LLM service.

## Known pitfalls

- Report and memory checks must preserve flow signs, rounding, units, category
  scope, explicit historical references, and mixed stock/sector prose. Pair each
  new rejection with valid-output checks; indicator labels are not company names.
  Fetch time does not certify observation time, and zero quotes are unavailable.
  Company identity columns in tables remain identity claims inside conditional
  plans; generic indicator columns are not names. Opening-market assertions need
  dated post-open quotes. Shadow comparisons preserve production context order
  and distinguish generated drafts from reviewed, publishable reports.
  Report correction receives the market receipts that triggered its checks;
  a dated price bar cannot supply the missing date of an industry snapshot.
- Explicit dates constrain memory recall without removing historical records.
  Autonomous runs read the exact-day morning workflow artifact with its full
  caveats and latest failure state; it is research context, not a trading order.
- A missing item in a Top-N snapshot does not prove sector retreat or outflow;
  comparisons need compatible dates, providers, and classification scopes.
- Research patterns must retain tool-verified instrument identity and observation
  provenance. Repeated searches or Skill reads are not independent trade outcomes;
  every cited day needs matching observations before a pattern can be persisted.
- Count pattern evidence days and select daily archive sources in the configured
  scheduler timezone. Preserve raw UTC timestamps, observation IDs, and legacy
  naive calendar dates; crossing UTC midnight alone is not a second local day.
- Promotion counts and learning summaries must distinguish accepted memory writes
  from archived or rejected review results; a completed review is not an adoption.
- Memory review commits use store-owned snapshot versions and atomic concurrency
  checks. Models judge content by exact entry ID; any returned version must still
  match. Repeated recalled content must not crowd distinct evidence out of ranking.
  Background completion must compare the original versions plus its own accepted
  write receipts; concurrent edits remain eligible for review. Persist each source
  task before serializing execution so a busy worker cannot discard another input.
  Review nudges belong to each conversation and survive new Web runtime instances
  and restart. Learning archives must be indexed when published; stopping learning
  after the main answer preserves that answer without appending it again.
- Keep new review batches bounded while reusing valid older batch checkpoints.
  Direct memory model calls honor the configured agent budget and earlier parent
  deadline/cancellation; an HTTP read timeout alone is not a wall-clock budget.
  Timeout and cancellation must reach a visible terminal state without unbounded
  thread joins, including reflection preflight. Check the execution budget at
  trade and learning publication boundaries, and reject late worker results.
- Model reasoning used across a tool sequence is transient operational context.
  Count its input tokens separately from recent-source protection, and keep it
  out of public session records and factual summary inputs.
- Trading rejections are execution outcomes, distinct from data availability.
  Preserve the actual rejection reason without reporting a functioning tool as
  unavailable or treating queried historical trades as new executions.
- Reports and learned profit claims use authoritative FIFO net settlements,
  including allocated entry and exit fees, and verified post-trade balances.
  Quantity/price annotations are not company-name claims; retain checks for
  genuine explicit instrument-name mismatches without triggering false repairs.
  Account capital is distinct from available cash. Verify monetary aggregates
  from account balances, retain unknown balances, and exclude decimal amounts
  and timestamp fractions from instrument-code extraction.
- Report checks preserve signed and grouped amounts, distinguish transaction
  counts from compared amounts, and retain exchange prefixes such as N/C/*ST
  in verified instrument names. Single and batch data retries resolve only the
  matching instrument and timeframe; another successful query cannot hide a gap.
  Retain dated OHLC evidence for historical claims, scope anaphoric dates to their
  clause or section, and distinguish tool calls/metadata from explicit company
  names. Review and correction use the same fee-inclusive FIFO allocations;
  returned allocation totals never imply full account history.
- Tool receipts distinguish fetched counts from truncated detail windows. Preserve
  existing count meanings, declare returned coverage, and derive full ledger counts
  from the ledger. Volume units require source evidence; unknown is not shares or
  lots. Synthetic observations remain sample evidence across API and agent tools.
  Host development instructions must not become workbench reply content.
- Daily journals, close analyses, daily reflections, and weekly strategy reviews
  use distinct execution sessions per run. Keep prior sessions searchable, and
  verify actual current-turn reads before
  accepting claims that historical tool results are current facts.
  Review factual claims against current receipts before publication; report
  correction must not repeat trades or other mutations.
  Deterministic checks must distinguish explicit current facts from dated history,
  anaphoric references, and conditional or future thresholds. Keep corrections
  bounded by the original deadline and recheck the same receipts before publication.
- Review totals come from the full dated run ledger, not a capped recent-detail
  list. Preserve trigger types and actual failure reasons in every review path.
  A planned stop price is not evidence of an accepted or executable stop order.
- Exact Chinese memory phrases must reach the matching record before loose FTS
  matches. Verify the actual tool result after updates and reopen, not only index presence.
- Editable builds and release builds have different frontend behavior.
- Updating source does not reload an already running service. After service
  changes, restart through its existing supervisor and verify the new process
  and affected task entry point. Do not mix freshly imported handlers with
  cached modules or replay missed autonomous trades to validate a restart.
- Native Agent adapters preserve application-owned history and business-tool
  authorization. Native resume handles are derived state: bind them to the
  published transcript and runtime/tool profile, serialize same-session writers,
  and never replay a submitted turn to recover a missing or uncertain handle.
  Verify real thread reuse across process restart separately from text recall.
  Keep application context compaction enabled, and bound
  subprocess request writes as well as response reads by cancellation/deadlines.
- SSE transport failures belong to reconnect handling, not agent failure
  messages. Close streams on completed, interrupted, or failed terminal events;
  verify cancellation before the first response delta in the actual UI.
- Scheduled model steps need a budget for multiple tool rounds with the configured
  model. Verify inference profiles, output reserves, and transient reasoning for
  every supported model after a switch. Scope tools to the task; preserve evidence checks.
- Native tool-call limits and API model-round limits are distinct settings.
  Budget exhaustion must retain receipts, disclose partial completion, and prevent
  automatic learning from an incomplete run. Summary calls share the original
  deadline and cannot replay mutations; cancellation and deadlines propagate into
  nested tools. An interrupted tool's outcome is unknown until verified, not proof
  that no mutation occurred.
  Nested research workers inherit the parent execution context and retain partial
  completion through final reports. Image analysis requires an actual image
  transport; text-only native responses cannot stand in for visual evidence.
  Ordinary specialist errors and empty results also retain incomplete status through
  dispatch, HTTP and learning gates. Serialize shared SQLite schema/WAL initialization
  across cold concurrent sessions without serializing their model execution.
  Explicit sample/synthetic quotes cannot become market-price fills, even if their
  prices, timestamps and volumes otherwise pass execution checks.
- Premarket plans may use the exact previous trading session's dated close as a
  planning reference. Keep it separate from last-trade fallbacks and live valuation;
  reference coverage cannot make a price fresh or relax execution quote checks.
  Missing, older, undated, or sample data still constitutes a gap. Report recovery
  appends a verified revision while preserving the original run status and receipts;
  it must not replay trading, learning, or delivery.
- `.trade-compass/skills/` becomes
  `trade_compass_agent/builtin_skills/` inside a wheel.
- Source and packaged configuration defaults are separate files.
- TestPyPI must not be used as a dependency index; download only this project's
  wheel there, then resolve dependencies from production PyPI.
- A successful upload is not release acceptance. Install the published version
  and exercise its entry point and packaged assets.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## 5. Preserve Product Contracts

**A technically successful change is still wrong if it violates the user's mental model or an existing product contract.**

For non-trivial state-changing work, define before implementation:
- The observable user outcome.
- The existing contracts that must remain true.
- High-impact assumptions and how they will be verified.
- One counterexample where the implementation succeeds but the user outcome fails.
- Consumer-facing acceptance checks.

For changes involving persistence, sessions, memory, channels, schedules, workflows, or migrations, trace:
```
producer → authoritative record → derived state/cache → consumer → user-visible surface
                                      ↓
                              restart/recovery path
```

Keep these roles explicit:
- **Authoritative user record:** what the product promises the user can continue to access.
- **Derived operational state:** summaries, checkpoints, indexes, caches, and other rebuildable data.
- **Archive/backup:** recovery material, not a substitute for normal product access.

Do not use an archive to justify removing data from a user-visible surface unless the user explicitly requested that behavior.

When learning from another codebase, write down the semantic mapping before adopting its mechanism:
- What user contract does the reference implementation assume?
- Which object in this project corresponds to it?
- What differs in lifecycle, UI, persistence, or recovery?
- What evidence would show that the mechanism is inappropriate here?

Verification must include the highest-level practical consumer available (API, UI-visible data, workflow result, or external behavior), not only internal storage or unit-level mechanics.

## 6. Keep User-Facing Copy User-Facing

**Describe outcomes and actions, not internal control flow.**

- Success messages should state the achieved result, actionable next steps, and
  risks the user must know.
- Do not expose implementation details, actions that did not occur, control-flow
  guarantees, or developer self-justification unless the user needs them to
  make a decision or recover.
- If an implementation fact changes no user choice or recovery step, omit it
  from the user-facing surface.
- Verify copy at the actual consumer surface, not only in source text.

## 7. Deliver the Product, Not Just a Working Development Setup

**A successful demo, green test suite, or merged PR does not establish that a
new user can use the shipped feature.**

### Make the supported entry point self-sufficient

- Before implementing a large feature, identify its normal installation and
  startup path, external prerequisites, writable state, and observable success
  condition. Include setup, first use, failure, restart, and upgrade in scope
  where the feature depends on them.
- Probes, temporary servers, manually copied assets, local toolchain paths,
  pre-authorized accounts, and cached certificates are development aids. They
  must not become hidden prerequisites of the delivered product. Convert any
  required step into the supported build, startup, or onboarding path and
  document genuine prerequisites.
- Preserve the agreed delivery model. A separately hosted demo, alternate
  service, or manually managed process is not a substitute for the project's
  own entry point unless the user explicitly changes that requirement.
- After a feature is merged, base follow-up fixes on current main and use the
  user's designated active checkout. Do not keep a retired worktree or its
  running processes as the authoritative implementation. Confirm the actual
  process, checkout, configuration, data root, and served assets before editing
  or restarting; preserve unrelated work in that checkout.

### Verify first use separately from continued use

- Test first use with isolated, fresh writable roots and no prior authorization,
  certificates, generated assets, or browser cache that would skip setup. Never
  obtain a clean test environment by deleting the user's existing state.
- For affected paths, separately verify continued use, process restart, upgrade,
  and recovery from interrupted setup or network loss. A successful run with
  existing credentials or certificates does not validate their initial creation.
- Check zero, one, and multiple records when availability of an action depends
  on data. Do not accidentally limit creation to an empty state. Keep unresolved
  startup state distinct from confirmed signed-out, unpaired, or empty state so
  existing users are not briefly shown the wrong onboarding flow.
- A mock certificate or provider response can verify application behavior, but
  cannot prove real authorization, certificate issuance, or public reachability.
  Use bounded real integration checks for those claims; exercise repeated
  failures with mocks or a provider sandbox instead of consuming production
  quotas through trial and error.
- Ask for device-only checks only when they resolve a specific unanswered
  acceptance criterion. Explain the expected observation, retain the result,
  and do not repeat an accepted check without a relevant change or new failure.

### Verify the artifact the user actually runs

- Build and install the distributable outside the repository, verify the
  imported package location, and exercise its normal entry point with fresh
  state. Confirm that required UI assets, helper executables, defaults, and
  licenses are included, with no accidental dependency on the source tree or
  developer-only tools. Apply this gate when changing distribution or startup.
- Trace UI changes through source, build output, serving process, browser cache,
  and service worker. Verify the loaded version before declaring the change
  visible. For update changes, test cold launch and an already active page
  separately, including preservation of drafts and the current session.
- Keep source and installed behavior aligned through the same implementation.
  Do not repair only the local running instance or rely on manual file copying
  as the permanent installation or upgrade mechanism.

### Treat failure and recovery as product behavior

- Define the success evidence, timeout, cancellation, failure state, and recovery
  action for each external setup step. A live process, open listener, or heartbeat
  must not be presented as successful authorization or usable connectivity.
- Keep status observable during slow operations. Once an operation has failed,
  stop presenting it as preparation; show its user-relevant consequence and an
  action the implementation can actually perform.
- Give retries an explicit owner and policy. Bound attempts, back off repeated
  failures, honor provider retry deadlines, and preserve applicable cooldowns
  across restart. UI retries and lower-level retries must not multiply requests
  or bypass provider limits.
- Retain safe diagnostic evidence across failure and restart: stage, timestamps,
  structured error category, and retry deadline where available. Preserve enough
  information to distinguish the original failure from a later timeout or rate
  limit, without logging credentials, private payloads, or authorization URLs.
- Recovery must preserve identity, authorization, pairings, user history, and
  stable application addresses. Do not reset them, replace accounts, weaken TLS,
  or create a new origin merely to make a failing test pass. Explain and obtain
  authorization for any necessary change to those product contracts.

### Make completion claims match the evidence

- Record which user outcome each check proves, whether it used fresh or existing
  state, and whether external behavior was real or simulated. Test counts and
  compilation success cannot substitute for the critical user journey.
- Fixing a spinner, adding a retry button, or improving diagnostics does not fix
  the underlying connection failure. Report those improvements separately from
  whether the original outcome has actually succeeded.
- Do not call a feature complete or ready for release while its critical first-use
  path is failing or unverified. State the remaining gap and the observation that
  would close it; merging a PR or an upstream issue report does not close it.
- Turn escaped defects into regression checks at the consumer boundary and
  durable rules here. Keep incident transcripts and temporary investigation
  details under `temp/`; avoid embedding machine-specific workarounds or one-off
  provider settings in these guidelines.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
