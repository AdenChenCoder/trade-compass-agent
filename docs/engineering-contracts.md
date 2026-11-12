# Engineering contracts by domain

Read the sections affected by a change before implementation. These contracts
define business meaning and lifecycle boundaries; they do not certify that a
development feature has shipped. Concrete accepted and rejected examples belong
in the linked regression tests, not in expanding prose exception lists.

## Report and market evidence

- Validate typed facts against runtime-owned receipts. Preserve instrument and
  account identity, metric, sign, rounding, unit, category, period and source
  scope through nested research, dispatch, publication and persisted output.
  Agent summaries, activity previews and model-supplied reader text cannot replace
  original evidence. Receipts belong to the exact run and instrument.
- Keep business requirements separate from execution and presentation policy.
  A report may reference raw facts, direct observation aliases and validated calculations; retain the original
  operands and resolve references independently of document order. Provenance
  does not establish semantic support, and model approval cannot clear a
  deterministic evidence conflict.
- Distinguish current observations, dated history, conditions and future plans.
  Dates apply to their clause or section, not unrelated claims. Retrieval time
  cannot establish observation time; opening-market claims require dated
  post-open quotes. One dated source cannot supply another source's missing date.
- A successful query only resolves the matching instrument, period and data gap.
  Counts of fetched records, returned details and full-ledger totals have distinct
  meanings. Truncated or Top-N windows cannot establish complete coverage, sector
  disappearance or outflow. Comparisons need compatible dates, providers and
  classification scopes. Unknown volume units remain unknown.
- Validate explicit identity and numerical claims without treating surrounding
  descriptions, metadata or conditional syntax as different identities or facts.
  Preserve verified names and scopes. Keep language variations and their invalid
  counterparts in regression fixtures rather than making wording the contract.
- Keep execution outcome, evidence availability and report completeness separate
  through workflows, API, sessions and UI. Preserve valid content beside unresolved
  items. Data gaps and unsupported conclusions remain incomplete; they do not
  imply execution failure. Incomplete output must not become automatic learning.
- Publication and correction use the same original receipts and deadline.
  Correction is read-only and cannot repeat business mutations. Recovery appends
  a verified revision while preserving original status and evidence; it does not
  replay trading, learning or delivery. Frozen comparisons preserve production
  context order and distinguish drafts from publishable reports.

Regression entry points:
[publication](../tests/test_report_publication.py),
[scheduled consumer](../tests/test_report_scheduler_contract.py),
[persisted consumers](../tests/test_report_incident_annotations.py),
[identity variations](../tests/test_report_identity_boundaries.py),
[dated claims](../tests/test_report_claim_scopes.py),
[market observations](../tests/test_report_bar_facts.py),
[tool citations](../tests/test_report_tool_citations.py).
Use the model-report acceptance procedure in
[development and production](development-production.md) before release.

## Trading and account meaning

- Trade rejection is an execution outcome, not evidence that a tool is unavailable.
  Historical trade reads are not new executions. A planned stop price is not an
  accepted or executable stop order. Interrupted writes have unknown outcomes
  until verified; neither replay nor a claim of no mutation is justified by timeout.
  Execution receipts describe completed or rejected operations, not new directional
  advice; recommendations alongside them still require market evidence.
- Profit claims use authoritative FIFO net settlements with allocated entry and
  exit fees and verified post-trade balances. Review and correction use the same
  allocations; a returned subset cannot imply full account history. Available cash
  and account capital are distinct, and unknown balances cannot become zero.
- Zero quotes are unavailable observations. Sample/synthetic quotes cannot become
  executable market prices; sample provenance survives API and tool transport
  even when other fields look valid.
  Premarket plans may use the exact previous trading session's dated close as a
  planning reference, separately from last-trade fallbacks, live valuation and
  execution freshness.
  Missing, older, undated or sample references still constitute gaps.
- The exact-day morning workflow artifact is research context, not a trading
  instruction. Autonomous runs preserve its caveats and latest failure state.
  Review totals come from the full dated run ledger and retain trigger types and
  actual failure reasons rather than inferring totals from recent detail windows.

Regression entry points:
[execution reports](../tests/test_execution_report_quality.py),
[account totals](../tests/test_report_cash_totals.py),
[settlement scope](../tests/test_report_claim_scopes.py),
[premarket references](../tests/test_premarket_reference.py),
[quotes](../tests/test_sina_quotes.py).

## Memory and learning lifecycle

- Explicit dates constrain recall without deleting history. Exact phrase matches
  must reach the matching record before loose search matches. Repeated recalled
  content must not crowd out independent evidence; verify returned content after
  updates and reopen, not just index presence.
- Learned patterns retain verified instrument identity and observation provenance.
  Repeated searches and Skill reads are not independent trade outcomes. Each cited
  evidence day needs matching observations. Count days and select archives in the
  scheduler timezone while preserving raw UTC timestamps, observation IDs and
  legacy naive calendar dates.
- Count accepted writes separately from archived or rejected reviews. A completed
  review is not adoption. Commit against store-owned snapshot versions atomically;
  model judgments identify exact entries, and any returned version must match.
  Completion checks include original versions and the review's own write receipts
  so concurrent edits remain eligible.
- Persist each source task before serializing review execution. Busy workers must
  not discard inputs. New batches stay bounded while valid older checkpoints
  remain reusable. Review nudges belong to each conversation and survive new Web
  runtimes and restart. Published learning archives are indexed; stopping learning
  preserves the main answer without duplicating it.

Regression entry points:
[recall](../tests/test_memory_recall.py),
[review progress](../tests/test_memory_review_progress.py),
[concurrent review](../tests/test_background_review.py),
[learning evidence](../tests/test_learning_evidence_boundaries.py),
[archives](../tests/test_learning_archive.py),
[persistent nudges](../tests/test_review_nudge_persistence.py).

## Execution budgets and sessions

- Parent deadlines and cancellation reach nested tools, research, memory review,
  reflection preflight, summary calls and subprocess writes as well as reads.
  An HTTP read timeout alone is not a wall-clock budget. Avoid unbounded thread
  joins, reject late worker results and check the budget at trade and learning
  publication boundaries. Terminal states must remain visible.
- Native tool-call limits and API model-round limits have distinct meanings.
  Scheduled work needs multiple tool rounds and an output reserve for the selected
  model. Verify supported inference profiles after a model switch. Scope tools to
  the task without relaxing evidence checks. Budget exhaustion retains receipts
  and partial completion; summary and repair share the original deadline and
  cannot replay mutations. Specialist errors and empty results stay incomplete
  through dispatch, HTTP and learning.
- Journals, close analyses, reflections and strategy reviews use distinct sessions
  per run while old sessions remain searchable. Current-fact claims need actual
  current-turn receipts. Serialize shared SQLite schema/WAL initialization for
  concurrent cold sessions without serializing their model execution.
  Concurrency tests wait for owned workers and durable terminal records rather
  than arbitrary short sleeps; release blocked workers before removing model mocks.
- Each scheduled execution starts with fresh model context, including native
  adapters. Daily session IDs and append-only user-visible history remain stable;
  previous receipts or answers cannot replace the current execution's evidence.
- Tool-sequence reasoning is transient operational context. Account for its input
  tokens separately from recent-source protection; exclude it from public session
  records and factual summary inputs.

Regression entry points:
[budgets](../tests/test_native_budgets.py),
[model limits](../tests/test_native_budget_config.py),
[review sessions](../tests/test_review_session_isolation.py),
[cold concurrency](../tests/test_session_cold_start.py),
[review execution](../tests/test_review_execution_context.py),
[reasoning context](../tests/test_in_turn_compaction.py).

## Native adapters and visual inputs

- Application transcripts and business-tool authorization remain authoritative.
  Native resume handles are derived state bound to the published transcript,
  context checkpoint and runtime/tool profile. Serialize same-session writers;
  never replay a submitted turn to recover an uncertain handle. Verify real native
  thread reuse across restart independently of text recall.
- Keep native runtime history out of the host Codex chat list. Check both histories
  after real calls and restart. Preserve credentials, credential-storage policy and
  legacy thread ownership when isolating runtime storage. Credential references
  must support backup/export without copying host secrets; preview and restore
  preserve both application and native records.
- Keep application context compaction enabled and deliver rewritten checkpoints
  to the native model. A stopped or expired preflight retains the submitted question
  and a visible terminal state without starting the business turn.
- Visual claims require actual image transport. Validated immutable image assets
  and original requests survive restart and compaction. Disable the vendor
  image-file tool: explicit inputs and application tools define image access.
  Codex chat images go directly to Codex; auxiliary vision settings continue to
  govern image tools. Text-only responses cannot substitute for visual evidence.

Regression entry points:
[native runtime](../tests/test_native_runtime.py),
[native sessions](../tests/test_native_agents.py),
[image inputs](../tests/test_native_image_input.py),
[vision boundary](../tests/test_native_vision_boundary.py).

## Client input and event transport

- Preserve attachment selection order across asynchronous reads. Wait for all
  reads before submission and discard stale reads after conversation changes.
- SSE transport errors belong to reconnect handling, independently of Agent
  execution failures. Close streams on completed, interrupted and failed terminal
  events. Verify cancellation before the first delta in the actual UI.
- Development instructions do not belong in workbench replies. User-facing states
  preserve drafts, authorization and current work across updates; unresolved
  startup state must not masquerade as confirmed empty or signed-out state.
- Failed lazy navigation keeps the current page, drafts and attachments mounted.
  Recovery cannot automatically reload or replay a task. Verify retry and abandoned
  navigation with the actual built bundle.
- Mobile helpers preserve supported local system proxies across process boundaries,
  including explicit empty overrides, without forwarding unrelated credentials or
  weakening TLS and routing-loop protections. Verify discovery after full startup,
  the actual child environment, cold connection and reconnect; a listening helper
  or mocked proxy dictionary does not prove that the original paired phone works.

Verification entry points:
[Web interaction tests](../apps/web/src/routes/AgentPage.sync.test.tsx),
[route recovery](../apps/web/src/lib/route-recovery.test.tsx),
[mobile recovery tests](../apps/mobile/src/App.recovery.test.tsx),
[mobile proxy](../tests/test_mobile_proxy.py),
[mobile PWA validation](mobile-pwa-validation.md).
