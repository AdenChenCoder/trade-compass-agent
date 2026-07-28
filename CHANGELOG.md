# Changelog

This project follows [Semantic Versioning](https://semver.org/) and records user-visible changes here.

## [Unreleased]

## [0.2.0rc5] - 2026-07-28

### Changed

- Replaced the numbered setup questionnaire with a keyboard-driven TUI:
  arrow-key navigation, Space-based multi-select, masked secret input, visible
  progress, and one-step-at-a-time confirmation.

## [0.2.0rc4] - 2026-07-28

### Added

- Guided `trade-compass setup` / `trade-compass configure` onboarding for LLM
  credentials, storage, market data, schedules, messaging, search, and privacy,
  with masked secrets, rerun-safe defaults, and local recovery copies.
- Explicit `--non-interactive` setup for automation and `--wizard` opt-in for
  source checkouts.

## [0.2.0rc3] - 2026-07-27

### Added

- Goal-oriented documentation for installation, configuration, CLI usage,
  Skills, architecture, and release operations.
- Canonical resource/action CLI groups, a machine-readable command catalog, and
  the `/api/agent/commands` integration endpoint.
- Community support and conduct policies plus categorized GitHub release notes.
- Installed-wheel and post-publish acceptance checks for packaged Web, schema,
  workflow, specialist, configuration, and Skill assets.

### Changed

- Existing flat CLI commands remain available as compatibility aliases while
  documentation uses grouped commands such as `data check`, `jobs run`, and
  `memory reindex`.
- Skill discovery now parses YAML frontmatter correctly, blocks reference path
  traversal, and gives a same-named writable runtime Skill precedence over its
  built-in default.
- Repository agent instructions now document product contracts, extension
  boundaries, package/runtime state ownership, and installed-wheel validation.
- Logging now suppresses verbose Lark SDK connection messages and redacts URL
  query strings, credential-shaped key/value pairs, and bearer tokens.
- CI now declares a stable Ruff rule contract and uses Node 24-compatible
  GitHub Actions across verification and release workflows.

## [0.2.0rc2] - 2026-07-26

### Changed

- Removed the unused DuckDB runtime dependency, reducing a clean installation by
  roughly 44 MiB while preserving the default Web, market-data, search, and LLM
  feature set.
- Added compatible major-version bounds to base dependencies so fresh installs
  do not silently adopt untested breaking releases.
- Corrected the OpenAI-compatible provider error message: the OpenAI SDK is a
  required base dependency because it powers the default DeepSeek provider.

## [0.2.0rc1] - 2026-07-22

### Added

- Scriptable `trade-compass service verify` readiness gates with human and JSON
  output, definition-drift detection, and nonzero failure exits.
- Bounded macOS launchd stdout/stderr retention (10 MiB per file, five archives)
  without touching audit, session, memory, or job history.

- Installed-app setup and readiness commands: `trade-compass setup`, `trade-compass doctor`, and `trade-compass --version`.
- Stable installed-app configuration and data root under `~/.trade-compass`.
- Production wheel verification for bundled UI, schemas, workflows, specialists, and safe defaults.
- Local-only bind enforcement, DNS-rebinding/CSRF defenses, request-size limits, and sensitive-file permission checks.
- Versioned, checksummed local backups with safe restore previews, automatic pre-restore rollback archives, and merge-only recovery.
- Private portable migration archives with normalized paths, known-credential exclusions, config redaction, dry-run imports, and automatic rollback backups.
- Linux systemd user-service lifecycle support with journal logging, linger diagnostics, and combined manager/TCP/HTTP health status.
- Initial public alpha release candidate.

### Changed

- Release builds now fail when the production web UI cannot be built instead of packaging a placeholder page.
- OpenAI-compatible client support is installed with the base package because the default DeepSeek provider requires it.
- Unverified inbound HTTP callbacks now fail closed; authenticated gateway connections remain available.
- User MCP configuration now follows `TRADE_COMPASS_HOME` consistently.
- Service definitions preserve non-secret custom runtime locations across login/reboot while leaving API keys in the protected `.env`.
