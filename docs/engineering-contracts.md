# Engineering contracts by domain

Read the sections affected by a change before implementation. These contracts
define business meaning and lifecycle boundaries; they do not certify that a
development feature has shipped. Concrete accepted and rejected examples belong
in the linked regression tests, not in expanding prose exception lists.

## Report and market evidence

- Reading acceptance permits minor awkward wording, redundancy or ellipsis when
  the surrounding body makes the business meaning unambiguous. Polished prose
  is not a completion gate. Preserve factual identity, dates, values, units,
  scale, direction and execution state; ambiguity that changes their meaning is
  not a cosmetic defect. Verify downstream consumers separately: original
  receipts and typed results remain authoritative, while narrative supplied to
  later reasoning or learning must not silently become a new factual source.
- Evidence structure is not the reader's writing structure. Published paragraphs
  may combine observations and judgments naturally. Explicit inline bindings keep
  values, units, identity and dates runtime-owned; a semantic review checks their
  use in the expanded sentence against original receipts and task scope. The explicitly named
  `value_with_unit` field binds a number and its unit as one indivisible display
  value; a percentage must not lose its percent sign. Author-written facts or
  units still require semantic review. The body must establish the units of
  author-written quantities; evidence units cannot silently fill a missing unit
  or rescale an unqualified ratio as a percentage. Reference
  existence alone does not establish meaning, and prose keyword/regex rules are
  not a substitute for this review.
  Composition receives source context alongside the typed catalogue: deterministic
  classifications and source limitations must not disappear. A labelled writing
  view may omit exact numeric fields already represented in that catalogue,
  retaining unindexed values, strings, units, list positions and genuine empty
  states. Such omissions cannot establish absent data. Independent review and
  saved artifacts retain the full original records. Report context checks use
  the available native token counter and this request's existing response cap,
  including stricter parent caps, without changing the loaded model window.
  Computed moving-average alignment is also a typed categorical fact, retaining
  the original receipt, observation time and indicator scope. Its reader display
  uses the business category; the raw source value and historical records remain
  unchanged. Calculation and publication share the same alignment operation,
  including its existing equality and insufficient-data behavior.
  Writer input retains the raw category alongside its runtime-owned display
  value and binding example. Business prose expresses that display value,
  rather than narrating tool calls or exposing internal enums. A correction
  preserves useful analysis and necessary answers without unrelated expansion;
  a stale observation used in a present-dated report retains its explicit source
  date. No post-review replacement or shortened incomplete answer repairs it.
  These records provide context, not another fact-reference namespace; original
  receipts remain unchanged and numeric bindings still use the fact catalogue.
  Numeric field meaning belongs to its producer. A chip-distribution average or
  cost interval is a dated estimate, separate from an account's holding cost or
  an observed transaction price. Preserve its method and scope through bindings,
  review and saved reports; a change in that estimate is not a stock return.
  A supplied distribution estimate may support ordinary technical language about
  a concentration/resistance area without repeating its method in every reader
  sentence. Preserve the estimator, date and scope in the owned sources. An
  ordinary price high alone cannot establish historical concentration, and an
  estimate cannot become a measured profile or exact frequency maximum.
  Apply measurement requirements to asserted measurements, not to bounded
  estimate-backed research interpretations. A general analytical lookback is
  not an additional calendar-date commitment; specific observation dates and
  relative calendar references retain their original temporal obligations.
  Choosing numeric bindings does not withdraw other owned sources from review.
  Original announcements, source qualifications and successful first-party read
  results remain available, including an empty result with no numeric rows.
  Model drafts and excluded sample/error sources do not become evidence.
  Large batch views still retain only explicitly selected instrument subtrees;
  source visibility cannot replace a bound value's original observation date.
  A first-party exit scan is also an exact quotation of its returned record,
  including empty or missing fields. Its run ownership anchors a statement
  about that scan's return, without inventing a market observation date or
  proving market freshness, risk absence or trading outcomes. Source-only
  quotation IDs remain available without a numerical paragraph binding;
  they cannot waive dates of adjacent market observations or widen batch views.
  Adjacent bound fields retain a runtime formatting separator, so a date and
  amount cannot concatenate into a different token. This whitespace is part of
  the exact reviewed and saved body; it supplies no factual or connective meaning.
  Percentage display uses runtime-owned decimal rounding to two places when the
  magnitude is at least 0.01 percent, with half-even ties. Smaller nonzero values
  retain their original precision so presentation cannot turn them into zero.
  Review receives this display value alongside the unchanged raw fact; receipts,
  calculations and persisted factual values retain their original precision.
- Review the actual reader presentation. No model rewrites approved paragraphs
  afterwards. Save the final consumer text alongside evidence and review; runtime
  availability notices may be appended without changing reviewed prose. Sources
  may be disclosed on demand. Task coverage is assessed from the reader body;
  a requested result present only in source notes does not count as an answer.
  Material gaps and uncertain execution outcomes
  stay visible in the body. Legacy saved text remains readable without regeneration.
  The parser may discard an exact empty text token, which contributes no reader
  characters. Preserve every nonempty character, strict field validation, the raw
  response and the minimum-content requirement; an empty report is not a repair.
  Natural-writing wire parts use literal JSON strings and exact two-string
  `[ref, field]` pairs. Expand them into the existing canonical text/value objects
  before validation, review or persistence. Legacy object parts remain supported.
  Never parse bindings out of prose, guess malformed JSON values or bypass the
  final semantic review. Repair prompts use the same wire format and aliases.

- Natural paragraph writing uses the model's reasoning path, with bounded output
  and application-side schema validation. Non-thinking mode serves internal
  evidence serialization. Body interpretation and entailment use the local
  model's bounded reasoning path, clearing inherited formatting mode while
  preserving the parent's token cap, deadline and cancellation. Reasoning is
  private and cannot become reader text or evidence.
  The semantic response envelope is at most 16,384 tokens, including reasoning
  and the final structured response. A smaller caller cap still wins. This does
  not extend the parent deadline, permit more calls or allow an incomplete
  response to become an approval; normal scenes must still finish in time.
  Reader writing, body/factual review and task-coverage review explicitly request
  the Qwen 3.5/3.6 precision sampling profile (temperature 0.6, presence penalty 0)
  while retaining reasoning, token caps and cancellation. General chat and raw
  non-thinking maintenance keep their existing profiles. Scope the request through
  nested calls and restore it on failure; never mutate the loaded model's settings.
  This adapts the model's [official task-specific sampling guidance](https://huggingface.co/Qwen/Qwen3.6-35B-A3B),
  not a claim that a sampling profile guarantees factual correctness.
- Report composition and task-coverage review share a maximum of four model calls. Unused
  structural-repair capacity may fund one feedback revision and a fresh review.
  This applies to both reader paragraphs and internal evidence/learning documents;
  a revision retains the chosen document format and cannot discard useful verified
  observations or calculations when its correction remains incomplete.
  Fingerprint the complete candidate reader text before review filtering. An
  unchanged body cannot become an accepted correction through renamed item IDs
  or a different second verdict; retain the initial outcome and both attempts.
  Preserve required gaps through revision; unavailable review must retain the
  first verified evidence. Invalid optional gap-dismissal metadata cannot remove
  a gap or invalidate independently valid paragraph reviews. Preserve cancellation
  and the parent deadline, and never replay business tools to repair prose.
- An explicitly configured `llm.local_report_review_model` separates factual
  entailment from task coverage. It uses only local LM Studio, original receipts,
  typed calculations, current execution records and the final statements. The original
  task is supplied separately as requirements context: it can establish a requested
  decision rule, but cannot prove market/account facts, causal premises or execution.
  Drafts and coverage decisions are not evidence. The local assessor returns
  separate identity/scope, time, quantity and inferential-support checks for
  every assertion. The runtime combines them with the overall verdict; an
  unknown or contradictory dimension cannot be overridden by a blanket approval.
  Missing or malformed dimensions make review unavailable, not a correct
  rejection. Supply the observation's calendar date in the display timezone
  alongside its unchanged original timestamp. Current claims must respect that
  date even within a judgment; retrieval time and latest-available status cannot
  make a prior-day quantity current. Explicitly dated historical values remain
  usable. Before evidence is supplied, body interpretation separately fixes
  whether an observation is asserted as of the task's present date. Review
  distinguishes an explicitly established calendar anchor from present currency.
  Referring back to that date retains it; a sample's date or the report's topic
  does not itself assert that an observation is current. Preserve any additional
  present-time assertion even when an explicit date accompanies it. Review
  cannot reinterpret that commitment as latest available. Temporal evidence
  links must be runtime-owned observations already supplied in this report.
  Paragraph selection is not source ownership: comparisons may span securities,
  accounts, timeframes and observation scopes. Ownership alone does not prove
  semantic identity, relevance or support; these dimensions remain independent.
  Unselected facts from supplied non-batch sources retain their canonical IDs
  and original dates in review context. Approved review observations remain
  navigable in reader source notes; original text, bindings and receipts stay
  unchanged. Unavailable or rejected assertions cannot add approved sources.
  Numeric bindings remain tied to their original context and mandatory even
  if the assessor omits them. The runtime compares those
  source dates in the task's reference timezone with the task date and can withhold a statement despite an
  optimistic model verdict. A bound price-change interval uses its later
  operand's observation date without discarding the earlier operand. This market
  observation check preserves the existing ownership contract for account
  snapshots. Display dates keep the display timezone; a different displayed day
  cannot by itself establish that a task-time observation is stale or current.
  Date-only observations, including provider `YYYYMMDD` and ISO `YYYY-MM-DD`,
  keep their supplied calendar date without an invented instant. Newly derived
  evidence preserves this precision; original receipts and already saved
  reports are not rewritten. A present research opinion is not itself a new market measurement;
  its independently asserted empirical premises still need their own support.
  These checks stay in internal evidence, never reader copy.
  Composition receives an actual date-binding rendering example, including a
  calendar-crossing observation when available. Raw stored instants are not
  reader calendar dates. Revision feedback retains each original assertion,
  its completion state, dimension checks and runtime date comparison rather
  than flattening the date conflict into a paragraph reason. This feedback is
  repair context, never new evidence or a prewritten reader sentence.
  Body interpretation selects runtime-addressed original source units rather
  than reproducing long paragraphs. Ranges start at the first unit, continue
  without gaps or overlap and end at the last unit. The runtime restores exact
  original characters, including framing, negation, conditions and whitespace;
  addressing boundaries assign no semantic categories or truth verdicts.
  Body wire fields separately represent one complete operation state, claim kind,
  actual observation-date anchors and categorical indicator assertions. Explicit
  calendar anchors remain ISO dates; `task_now` preserves an additional present
  observation commitment and `unresolved` remains unknown. Provide runtime calendar
  and timezone context without task instructions or source evidence. A dated
  sample must not become current merely because it is an empirical observation.
  The operation code chooses a valid combination of
  meaning, outcome and scope instead of asking the model to cross-fill three
  related fields. Exact typed decoding maps into the existing canonical types
  before review and persistence; earlier boolean wire annotations, legacy selectors and
  exact quoted responses remain readable. Reject mixed field namespaces and
  illegal values rather than guessing a category or repairing reader meaning.
  Strictly reconstructed quoted responses remain readable for compatibility.
  This adapts the source-selection idea in the
  [W3C annotation model](https://www.w3.org/TR/annotation-model/#text-position-selector)
  and delimiter retention in [LangChain's splitter source](https://github.com/langchain-ai/langchain/blob/master/libs/text-splitters/langchain_text_splitters/character.py).
  Unlike retrieval chunks, these units have no overlap, trimming or content
  normalization, and belong to one immutable body snapshot.
  An invalid temporal reference makes that assertion unavailable, not false.
  The same row ownership applies to malformed assessment fields. Preserve valid
  sibling assessments, the original response and the unavailable row's causes;
  never coerce an invalid label into an approval or completed rejection.
  Missing, repeated or foreign assertion IDs invalidate the shared envelope.
  The same applies when review claims an entailed current observation time but
  supplies neither a source witness nor an existing runtime date binding.
  Qualitative observations still need their source date; absence of an explicit
  number does not make a present empirical claim atemporal. Unknown source time
  remains a substantive uncertainty when the review actually reports it.
  Persist this distinction and withhold its paragraph while preserving other
  independently verified analysis. A completed rejection elsewhere can justify
  the one existing genuine body revision; an annotation error alone cannot
  trigger a review retry. A conflict independently established by original bound
  dates or owned indicator operands may also justify that single changed-body
  revision while the model annotation remains unavailable. Exclude model-selected
  witnesses from this independent check; missing or ambiguous evidence is not
  a demonstrated conflict. Normal completion and learning still require all
  checks. An incomplete reader revision may retain independently approved
  paragraphs despite another paragraph's annotation error; withhold that
  paragraph and preserve the unavailable status, causes and prior supported
  business sections. Do not replace useful revised analysis with an older
  rejected body merely because its review envelope was complete.
  Each statement
  must be entailed; contradictory or unknown statements are withheld. Missing,
  malformed or unavailable assessments cannot fall back to writer approval.
  Assess actual factual commitments while preserving negation, conditions and
  modality. Evaluate qualitative descriptions within their stated observation
  scope; do not add an unstated claim of persistence, statistical significance
  or a stronger technical diagnostic. Those properties still require evidence
  when asserted. An evidence-scoped limitation concerns what the provided observations
  establish, not whether a future event is impossible. Non-empirical report discourse
  needs no market attribution; claims about available records still do. Disclaimers
  and opinions cannot exempt adjacent factual claims, causal premises or quantities.
  Before evidence review, the same local model independently partitions and types
  the body without task instructions, receipts or other source evidence. Exact
  text reconstruction is required; omission or rewriting fails this contract.
  Reuse these exact spans as independently assessed assertions, including a
  conclusion joined to otherwise correct measurements. Give each assertion a
  runtime-owned ID and require exactly one assessment per ID. A paragraph passes
  only when all its assertions pass. The factual assessor receives every exact
  original reader span in reading order, grouped by paragraph, so subjects,
  ellipsis and shared conventions retain their original context. Reader spans
  are untrusted claims, never evidence for one another. Do not generate copied
  or normalized referent hints as a second authority for reader meaning; legacy
  hints are ignored and never reach factual assessment. Source contexts contain only owned facts and
  calculations. Do not duplicate the candidate paragraph as apparent source
  text. Matching activity measurements cannot establish unobserved actors'
  actions or causes. Missing, duplicate or unknown assertion assessments fail
  the review rather than silently dropping a claim.
  Record each span's commitment type before evidence is supplied: observation,
  comparison, actor activity, causal relationship, judgment or discourse. Split
  spans when that type changes. An asserted actor identity/action or causal
  process cannot become a qualitative judgment merely because it accompanies
  standard technical analysis. The evidence assessor preserves that interpreted
  commitment and checks the corresponding actors, actions, dates and scope;
  a correct measurement cannot replace the required proof. These internal types
  are neither factual evidence nor reader-facing content.
  Entailment records whether support is direct source evidence, a validated
  calculation, bounded reasoning, absent or inapplicable. A body's asserted
  actor activity or causal relationship cannot be established by a calculation
  or analytical interpretation alone. Preserve that empirical commitment and
  withhold it even when the overall model verdict approves. Missing or invalid
  support metadata is unavailable review, not a completed factual rejection;
  preserve other independently approved paragraphs. This typed compatibility
  check does not establish sentence truth: a model can still misidentify the
  basis, so exact semantic counterexamples and legitimate paired reports remain
  mandatory. No prose phrases or keywords classify support in the runtime.
  The factual assessor independently records participant and causal commitments;
  either stage's positive commitment requires the corresponding direct evidence.
  Negative body classifications cannot exempt a statement from that assessment.
  Missing or malformed independent commitments remain unavailable review.
  Categorical moving-average assertions fixed before evidence retain their scope
  and equality/inequality predicate. Publication checks the actual owned moving
  averages using the calculation's operation, despite an optimistic model verdict.
  Bound observations cannot be replaced by another security or date selected by
  the assessor. Explicit period subsets do not assert the whole system's order;
  unavailable periods or ambiguous source scope remain unknown. Preserve these
  checks, raw verdicts and original assertions through revision and saved history.
  Correct extraction of meaning is still a model obligation, not established by
  an enum or a valid evidence reference; paired real-model and full-report checks
  remain mandatory.
  Pass the body-only operation meaning alongside the commitment type. Proposals
  and conditional watchpoints are not completed actions or measured predictions;
  verify their asserted premises and rule compliance without requiring execution
  receipts or a historical observation of the proposed threshold. Assess bounded
  research judgments through their factual premises and reasoning scope rather
  than rejecting them solely for being subjective. None of these distinctions
  exempts adjacent empirical, causal or participant claims from evidence review.
  An analytical assessment of a holding rationale is not an attribution of the
  investor's private beliefs or a completed strategy change. The time dimension
  checks empirical observation dates, not whether a proposed future action has
  already occurred. Pure proposals have no empirical time commitment; dated
  factual premises remain independently applicable and cannot be waived.
  This body-only review also identifies quantities whose units or numerical scale
  are not established by the reader context. Evidence review cannot supply a
  missing convention or override that finding. Explicit shared units and
  dimensionless valuation multiples remain valid; this is a semantic assessment,
  not a punctuation or keyword rule. A bounded revision may clarify the body.
  Runtime-owned value-with-unit spans establish unit presence independently of
  the model. Locating a quoted issue wholly within those spans may clear only
  the missing-unit allegation, never sentence truth or scope. A matching quote
  in author text cannot borrow another occurrence's binding. Persist this check
  and still require factual entailment for the complete sentence.
  Execution assertions concern an actor's state-changing operation (orders or
  memory writes), not observations of price movement, returns or holdings.
  Current execution assertions are removed from AI analysis before coverage and
  factual review. The runtime renders actual mutation receipts separately. Without
  current receipts, such assertions remain unconfirmed and the report incomplete;
  model approval cannot turn empty receipts into proof of non-execution. Historical
  statements, plans and uncertainty remain eligible for factual review.
  Reporting an operation and performing it have different semantics. Classify the
  business outcome the author commits to, including under confirmation; an operation
  mentioned within a presentation rule does not itself assert a business outcome.
  An authored prospective choice or recommendation remains a plan even when made
  in this run. Verify its factual premises and claimed compliance with supplied
  requirements; do not require execution receipts to prove the choice itself.
  Independently classify and check any accompanying execution assertion.
  Original and revised bodies share at most two body-meaning and two entailment
  calls, making at most eight model calls in total. The parent deadline, cancellation and no-tools boundary
  still apply. Record both review inputs/results and actual total calls. Choosing
  a model is not certification: the exact configured path must pass semantic,
  full-task, long-report and downstream learning acceptance before activation.
  Pass the runtime-owned business date and scheduler timezone separately from
  instructions, together with the renderer's display timezone. Preserve this
  context through revision and persistence. An unknown reference date stays
  unknown; neither retrieval time nor the latest source date establishes today.
  Historical reuse must retain dated source meaning rather than adopt the review
  machine's current date. Pair stale-as-current rejection with valid historical
  wording and UTC-to-local calendar-date acceptance.

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
- Model-declared gaps are semantic claims. The existing reviewer may dismiss a
  gap only when it is outside the task or cites validated report facts/calculations
  already providing the requested content. Numeric value bindings inside natural
  paragraphs count as validated report facts; identity-only bindings and bare
  references do not establish that a value was provided. Clearing a duplicate gap
  cannot approve unsupported prose or erase other findings. Preserve the original claim and review
  in the artifact. Missing/invalid reviews retain gaps; deterministic evidence and
  execution failures cannot be dismissed by this review.
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

- Automatic learning from verified task reports reopens the run-owned evidence
  artifact, checks its digest and completion state, and curates against original
  receipts through the bounded, read-only internal report contract. Human prose
  is context, never a replacement for receipts. Only complete curated results
  enter the searchable archive; failed attempts remain report diagnostics linked
  to the source turn. Missing evidence must not fall back to prose-only learning.
  This does not certify legacy interactive learning.
- Memory compaction selects complete source records rather than rewriting their
  factual content. Keep source paths and qualifications, exclude earlier compactions
  from new selections, and deduplicate identical extracts. Original records remain
  accessible. Unavailable/invalid selection may retain a bounded extract of recent
  records, never model-supplied replacement prose. It is historical recall, not
  fresh market/execution evidence. Indexed and fallback lookup preserve complete
  content within the consumer budget and explicitly mark oversized results;
  bootstrap context must not silently cut the qualifications off selected records.
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
- Local context preflight counts the complete prompt, tool definitions and private
  reasoning with the loaded model's tokenizer and actual loaded window. A byte
  limit on a tokenizer connection is not a token limit. Use bounded read-only
  transport that accommodates large valid prompts; never load, unload or predict
  to obtain a count. Preserve the parent deadline, output reserve and conservative
  fallback when measurement is unavailable. Do not omit source data or user
  history to make a transport limitation look like a fitting request.

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
