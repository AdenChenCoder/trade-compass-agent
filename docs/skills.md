# Skills

Skills are versioned, runtime-loaded instructions for repeatable business
workflows. They are not a replacement for Python tools: a Skill defines when
and how to work, while tools perform data access, calculation, and persistence.

## Lifecycle

```text
author in source or the writable runtime vault
  → validate and package built-ins
  → discover name + short description at the start of each agent turn
  → expose summaries to the agent
  → load full SKILL.md only when selected
  → load references only when requested
```

## Skill locations

| Location | Owner | Runtime role |
| --- | --- | --- |
| `.trade-compass/skills/` | Project maintainers | Editable built-in Skills in a source checkout |
| `trade_compass_agent/builtin_skills/` | Built wheel | Installed copy of built-in Skills |
| `memory_vault/skills/` | User and agent workflows | Writable runtime Skills |

The wheel build maps `.trade-compass/skills/` into the installed package.
Runtime-created Skills remain in the writable memory vault and are never written
back into package files.

## Directory format

```text
.trade-compass/skills/stock-research/
├── SKILL.md
├── references/
│   └── methodology.md
├── scripts/
└── assets/
```

Only `SKILL.md` is required. Keep the always-visible trigger description short;
put branch-specific knowledge and large examples in `references/`.

## SKILL.md contract

```markdown
---
name: stock-research
description: Use when the user asks for a structured company or stock analysis.
category: analysis
---

# Stock research

## When to use
## Inputs and prerequisites
## Workflow
## Output contract
## Failure handling
## Verification checklist
```

The directory name is the runtime identity. Its `name` frontmatter should match.
Descriptions are parsed as YAML, including folded values such as
`description: >`.

## Runtime behavior

At the start of each agent turn, prompt construction discovers current Skills
and injects only name, source, and description. The agent calls
`load_skill(name)` for the complete body and may then call
`load_skill(name, reference="methodology")`.

Reference names are single Markdown file stems. Path separators, `..`, and an
explicit `.md` suffix are rejected.

`config/agent_skills.yaml` controls the built-in allow-list, summary overrides,
and ordering. Writable `memory_vault` Skills remain discoverable so user-owned
procedural memory is not hidden by a project allow-list.

If a writable runtime Skill has the same name as a built-in Skill, the runtime
Skill overrides it. Discovery returns one unambiguous Skill for that name.

## Authoring checklist

- Describe observable trigger conditions, not marketing copy.
- End workflows with checkable completion criteria.
- Keep reusable process in `SKILL.md`; move large or rarely needed facts out.
- Name exact tools only when the workflow requires them.
- Avoid credentials, transient market conclusions, and unverified commands.
- Test both source discovery and installed-wheel discovery.
