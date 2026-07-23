# Changelog

This project follows [Semantic Versioning](https://semver.org/) and records user-visible changes here.

## [Unreleased]

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
