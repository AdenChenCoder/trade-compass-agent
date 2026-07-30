# Privacy

Trade Compass Agent is a local-first, single-user application. It does not
provide a hosted Trade Compass account, and the application contains no
first-party telemetry or analytics client.

Local-first does not mean offline. Data leaves the machine when a feature that
needs an external provider is used.

## Data kept locally

The configured application home normally contains:

- `.env`: LLM, market-data, search, and messaging credentials;
- `data_dir`: conversations, tool and workflow runs, audit records, paper
  portfolios, schedules, market caches, notifications, and channel state;
- `memory_dir`: user rules, durable knowledge, indexes, and runtime-created
  Skills;
- service logs under `data_dir/logs` on macOS, or the user systemd journal on
  Linux.

For an installed application, setup makes the application home, data
directory, and memory directory owner-only. Source-checkout setup makes the
configured data and memory directories owner-only. Secret-bearing
configuration and WeChat credential files use owner-only file permissions. A
local process running as the same operating system user can still read them.

Conversation sessions intentionally retain prompts, model replies, and tool
results so the Web UI, CLI, and channels can continue a session. Service logs
record operational metadata and errors, not message bodies; common credential
forms are redacted before application logs are emitted.

## Data sent to other services

Depending on configuration and the action requested:

- the selected LLM provider receives the conversation, system instructions,
  applicable user rules and memory, selected tool results, and supported
  attachments needed for the request;
- market-data and search providers receive symbols, date ranges, or search
  terms;
- URLs explicitly fetched by the agent are sent to their destination after
  local-network targets are rejected;
- Feishu, WeCom, WeChat, or generic webhook providers receive messages routed
  through those integrations;
- the optional Kronos forecast feature downloads model and tokenizer files
  from Hugging Face.

Ollama and LM Studio can keep model inference on a locally configured endpoint.
The `privacy.allow_external_llm_memory` setting controls the additional
LLM-based learning and memory-curation pass. It does **not** remove applicable
rules or memory from normal agent requests.

Each external service processes received data under its own terms and privacy
policy. Do not put credentials or information that the selected provider
should not receive into prompts, rules, memory, Skills, URLs, or attachments.

## Retention and deletion

Trade Compass does not automatically upload local state. Session cleanup may
prune scheduler sessions, but ordinary user sessions and memory remain until
the user deletes or replaces them through the product or removes the
corresponding local state.

Setup can create adjacent `*.setup.bak` files. `trade-compass backup` archives
configuration, credentials, data, and memory for recovery. Treat those
owner-readable archives as sensitive copies and delete them separately when
they are no longer needed.

Before sharing logs, bug reports, backups, screenshots, or exported state,
review them for account identifiers, portfolio information, message content,
provider responses, and credentials. Report suspected credential exposure
privately through [SECURITY.md](SECURITY.md).
