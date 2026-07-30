# Threat model

Last reviewed: 2026-07-30.

## Security objective

Protect a single user's credentials, research, portfolio state, rules, memory,
and channel identity while preserving the same local workflows in a source
checkout and an installed wheel.

## Trust boundaries

```text
local browser/CLI
       |
       v
loopback FastAPI process ----> configured LLM, market/search providers
       |                                  |
       v                                  v
data_dir + memory_dir              provider-controlled systems
       ^
       |
messaging gateways <---------- Feishu / WeCom / WeChat
```

The Python package and bundled Web UI are read-only application assets.
Writable state belongs under configured data and memory roots. Package
installation and GitHub release automation form a separate supply-chain trust
boundary.

## Assumptions and non-goals

- The operating-system account and machine are trusted. Malware or another
  process running as the same user is outside the application's protection.
- This release is single-user and loopback-only. It has no user accounts,
  remote authentication, tenant isolation, or role-based authorization.
- A reverse proxy does not make remote deployment supported; authentication,
  TLS termination, CSRF policy, rate limits, and channel authorization would
  need a separate remote-mode design.
- External LLM, data, search, model-hosting, and messaging providers are
  independent trust domains.
- Portfolio operations are local paper-trading records. Enabling a real broker
  or external order path requires a new authentication, confirmation,
  idempotency, audit, and recovery review.

## Principal threats and controls

| Threat | Current controls | Residual risk |
| --- | --- | --- |
| Remote or DNS-rebinding access to the local Web app | Loopback-only bind, loopback `Host` validation, local-origin checks | Same-user local processes can access the service |
| Oversized or streamed request bodies | Declared and actual body bytes are capped | Requests within the cap can still be computationally expensive |
| SSRF through user-supplied URLs | Public HTTP(S) destinations only, DNS resolution validation, connection pinned to the validated IP, redirect revalidation, HTTPS downgrade rejection, response cap | A permitted public site can still return malicious or misleading content |
| Prompt injection from pages, market data, files, or messages | Tool policy and human-owned rules outrank retrieved content; mutations remain in deterministic tools | Model output and research conclusions still require user judgment |
| Credential or message leakage in logs | Message bodies are not logged; URL queries, bearer tokens, key/value and JSON credential forms are redacted | Third-party exception text may contain an unforeseen secret shape |
| Local state disclosure | Owner-only application, data, memory, config, and channel-credential permissions | Backups, exports, screenshots, and systemd journal retention need user care |
| Channel impersonation or replay | Authenticated outbound gateway connections; legacy HTTP callback fails closed | Provider-account compromise is not mitigated locally |
| Dependency or release compromise | Locked dependencies, vulnerability audits, full-SHA Actions, pinned bootstrap tools, installer and release checksums, SBOM, installed-wheel smoke tests | Package registries, GitHub, and upstream maintainers remain trusted |
| Destructive restore or import | Preview by default, checksum validation, merge restore, recovery backup | User-confirmed replacement can still change persistent state |

## Security-sensitive change checklist

Re-review this model before adding any of the following:

- non-loopback listening, shared users, or browser access from another origin;
- a real brokerage, order-routing, or credentialed mutation API;
- inbound HTTP callbacks or a new messaging platform;
- a new place where prompts, rules, memory, files, or portfolio data leave the
  machine;
- executable plugins, model code, restore formats, or persistent migrations;
- React Server Components, SSR, or server actions in the Web application.

Use [SECURITY.md](../SECURITY.md) for private vulnerability reporting and
[PRIVACY.md](../PRIVACY.md) for the user-facing data-flow description.
