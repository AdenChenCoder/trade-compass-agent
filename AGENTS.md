# AGENTS.md

Trade Compass Agent is a local-first A-share research and trading workbench,
delivered as one Python package with bundled Web and mobile interfaces.
This file contains shared development rules and routes to task-specific contracts.

## Product contracts

Preserve these unless the user explicitly changes them:

- Source checkouts and installed wheels expose the same user journeys.
- Installed package assets are read-only. Writable state belongs under the
  configured data and memory roots.
- Human-owned rules and pinned memory outrank agent-created memory.
- Built-in Skills are versioned package assets; runtime-created Skills live in
  the writable memory vault.
- Web, CLI, API, scheduled jobs and services share authoritative business semantics.
  UI code presents backend behavior without duplicating its rules.
- Persistent restore/import operations preview by default and preserve recovery.
  Upgrades retain accounts, sessions, schedules, memory, credentials and pairings.
- Application history is a user record. Summaries, checkpoints, indexes and native
  resume handles are derived state; archives are recovery material. Neither
  derived state nor an archive substitutes for continued access to user history.

## Frame the problem before implementation

For non-trivial changes, state concisely:

- The user's observable outcome, separately from the symptom and proposed mechanism.
- The contracts that must remain true and any high-impact assumptions.
- A counterexample where the implementation works but the user outcome fails.
- Consumer-facing acceptance checks and a brief sequence of work.

Trace the relevant producers, authoritative records, derived state, consumers and
restart/recovery paths before changing shared state. If available evidence cannot
settle a choice that changes product semantics, identity, permissions, history,
cost or reversibility, ask before making that choice.

Use references as evidence, not ready-made answers. Map their assumptions and
lifecycle to this product, and seek examples that would falsify the proposed fix.
Stop and reframe if an internal metric replaces the user outcome or the full
user-visible impact cannot be explained.

## Implementation decisions

- Make the smallest coherent change that satisfies the contract. Avoid speculative
  abstractions, features or configuration, and preserve unrelated work.
- Fix failures at the responsible data, ownership or lifecycle boundary.
  Repeated wording-dependent failures call for a contract review, not an expanding
  list of prose exceptions.
- Base factual checks on typed business data and runtime-owned evidence. Keep
  business requirements separate from orchestration and presentation instructions.
  Traceable evidence does not by itself establish that a conclusion is supported.
- Preserve distinctions between execution failure, unavailable evidence and
  incomplete output through persistence and every consumer. Do not manufacture
  success by dropping checks or relabelling missing information.
- Add repeatable instructions/output contracts as Skills, deterministic operations
  as tools, focused reasoning as specialists and scheduled multi-step flows as
  workflows. Shell command metadata belongs in `command_catalog.py`; retain
  compatibility aliases when reorganizing commands.
- Remove unused code created by this change. Do not clean up unrelated code,
  formatting or unfinished work.

## Read the contracts for the affected area

Read the relevant sections before changing their behavior; do not load every
domain's history for an unrelated task.

| Task | Required reference |
| --- | --- |
| Code location, state ownership, package assets | [Architecture](docs/architecture.md) |
| Reports, research evidence, market facts or degradation | [Report and market evidence](docs/engineering-contracts.md#report-and-market-evidence) |
| Orders, prices, balances or profit claims | [Trading contracts](docs/engineering-contracts.md#trading-and-account-meaning) |
| Recall, memory review or learning | [Memory lifecycle](docs/engineering-contracts.md#memory-and-learning-lifecycle) |
| Scheduling, cancellation, budgets or session concurrency | [Execution contracts](docs/engineering-contracts.md#execution-budgets-and-sessions) |
| Native Agent adapters, compaction or image inputs | [Native adapter contracts](docs/engineering-contracts.md#native-adapters-and-visual-inputs) |
| Attachments, streaming or reconnect behavior | [Client transport contracts](docs/engineering-contracts.md#client-input-and-event-transport) |
| Service changes, previews, packaging, release or rollback | [Development and production](docs/development-production.md) and [Releasing](docs/releasing.md) |

## Verification and completion

- Changes affecting project task execution, Agent loops or runtime architecture
  must pass the local [execution protection acceptance](docs/development-production.md#执行链路变更的防护验收)
  before the Agent marks work complete, makes a PR ready, merges or releases it.
  This includes indirect changes to scheduling, tool permissions, model adapters,
  prompts/Skills, report evidence, shared configuration and state lifecycle.
  Define the affected scenes and pass criteria before implementation; run the
  applicable deterministic, installed-package and real-model checks and retain
  exact-artifact evidence. A missing check, unavailable model or failed normal
  scene blocks completion; record the blocker instead of substituting green CI,
  a mock or a reclassified failure. No dedicated CI runner is required.

- Turn defects into regression checks at the practical consumer boundary.
  Pair invalid-input rejection with legitimate-output acceptance. Vary wording
  and ordering without changing meaning, and evidence validity without relying
  on different wording. Verify the final API, workflow result or UI-visible state.
- Test first use separately from continued use where affected, using fresh isolated
  roots instead of deleting user state. Cover zero/one/multiple records when
  behavior depends on availability, plus failure, restart, upgrade and recovery.
- Mock model execution explicitly in unit tests; clearing API keys does not disable
  a local model. Workflow tests use isolated data and memory roots even with
  `persist=False`, since diagnostics and nested workflows can still write.
  Tests of configuration/default paths clear the relevant environment overrides;
  isolation must not mask the path-resolution contract under test.
- Repairs and retries have an explicit owner and bounded policy. Preserve parent
  cancellation/deadlines, cooldowns across restart and uncertain write outcomes.
  Do not multiply retries across UI and lower-level handlers.
  Do not replay mutations to regenerate a report or establish correctness.
- Keep safe diagnostics: stage, timestamps, structured cause and retry deadline
  where relevant, without credentials or private payloads. A running process,
  heartbeat or improving status display is not proof of functional recovery.
- Record what each check proves, whether state was fresh or existing, and whether
  external behavior was real or simulated. Test counts and green builds cannot
  replace the critical user journey. Ask for device-only checks only for a specific
  unresolved criterion; retain accepted results unless relevant behavior changes.
- Before declaring completion, report the observed outcome, unrequested behavior
  or data-semantic changes, consumer paths tested, remaining uncertainty and any
  recovery path. Do not claim readiness while a critical path is unverified.

## Development and stable service

- Confirm the actual executable, configuration, data, memory and served assets
  before editing or restarting a service. Development uses separate roots, ports
  and supervision. If a legacy service runs from source, work in an isolated copy;
  it may import changed modules later even without a restart.
- The stable service runs a reviewed, remotely published, versioned package.
  Local builds, editable installs and passing tests are not production releases.
  Validate the published artifact outside the checkout through its supported entry
  point before authorized cutover, following the linked release procedures.
- Preserve the existing supervisor and only one production writer. Cutover requires
  a consistent backup, retained previous program/service definition and verified
  compatibility with any new records before rollback. Never replay missed trades
  as validation or reset identity, authorization or history to make checks pass.
  Preserve stable addresses and transport security during recovery; changing
  these product contracts requires explicit authorization.
- After merging, base follow-up changes on current main and the designated active
  checkout. Preserve unrelated work; a retired worktree or preview process is not
  the authoritative implementation.
- Deliver through the supported installation/startup path. Temporary servers,
  copied assets, cached authorization or developer tool paths must not become
  hidden prerequisites. Verify package contents, actual loaded assets and restart
  behavior rather than assuming source edits reached the running service.

## User-facing content

- Development requests, plans, architecture choices and acceptance criteria guide
  implementation; they are not product copy.
- Show information needed to understand business data, act, recognize real status
  or recover. Do not add explanations of how the feature was built or claims
  proving task completion, including in tooltips and accessibility labels.
- Preserve meaningful warnings about unsaved, unsynced, sample or unavailable data.
  Keep unresolved state distinct from confirmed empty or signed-out state.
- Inspect affected rendered states, including loading, errors and recovery.
  Updates must preserve drafts and active work; report any unverified UI states.

## Maintaining project knowledge

- Add a rule here only if it changes a reusable decision across tasks and is not
  already covered. Global product contracts and navigation also belong here.
- Put scoped business contracts in the linked domain guide, procedures in their
  existing guides and concrete counterexamples in regression tests. Instructions
  do not enforce runtime correctness or substitute for a code fix.
- Temporary plans, investigation notes and ad hoc verification results belong in
  repository-root `temp/`, which stays ignored and must never be force-added.
  Incident IDs, machine paths and one-off provider workarounds do not belong here.
- Runtime reports, logs, caches, memory and recovery backups remain under their
  configured runtime roots. Classify files by producer and use, not by filename,
  format or age. Durable project documentation belongs in `docs/`.

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

Run installed-wheel checks outside the repository so Python cannot import
`src/` instead of the installed package. Select further checks from the affected
domain and release guides.
