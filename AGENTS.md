# AGENTS.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

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

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
