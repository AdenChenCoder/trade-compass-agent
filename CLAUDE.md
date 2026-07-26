# CLAUDE.md

The canonical repository instructions are in [AGENTS.md](AGENTS.md). Read that
file before changing code, configuration, persistent behavior, documentation,
or release automation.

Project-specific essentials:

- Preserve source-checkout and installed-wheel parity.
- Keep package assets read-only and runtime state in configured data/memory
  roots.
- Put reusable procedures in Skills, deterministic capabilities in tools,
  focused reasoning roles in specialists, and scheduled multi-step flows in
  workflows.
- Run the highest-level practical consumer check, including an installed-wheel
  check when packaging or bundled assets change.
