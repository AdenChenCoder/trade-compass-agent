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

## Known pitfalls

- Editable builds and release builds have different frontend behavior.
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
