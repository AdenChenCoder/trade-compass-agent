# Configuration

Trade Compass keeps installed-application state separate from package files.

```text
~/.trade-compass/
├── config.yaml
├── .env
├── data/
├── memory_vault/
├── backups/
└── mcp.json                 # optional
```

An installed application uses this layout. Run `trade-compass setup` to create
it and complete the guided configuration; run `trade-compass configure` to
revisit the same wizard later.
Existing secrets are retained when their masked prompts are left blank, and
changed files receive a neighboring `*.setup.bak` recovery copy.
The TUI uses `↑/↓` for navigation, `Space` for multi-select, and `Enter` to
confirm the current item.

In a source checkout, `config/default.yaml`, `.env`, `data/`, and
`memory_vault/` remain the development defaults. Setup only initializes those
files unless `--wizard` is passed explicitly.

## Installed package paths

`uv tool` chooses platform-specific package directories. Inspect them instead
of assuming a hard-coded `~/.local` path:

```bash
uv tool list --show-paths
uv tool dir
uv tool dir --bin
```

- `uv tool dir --bin` contains the `trade-compass` command.
- `uv tool dir` contains the isolated package environment; the application does
  not store writable user state there.
- `TRADE_COMPASS_HOME` defaults to `~/.trade-compass` and contains writable
  application state.
- `config.yaml` stores non-secret settings; `.env` stores credentials with
  owner-only permissions.
- `data/` and `memory_vault/` are the default data and long-lived memory roots.
- `backups/` contains recovery archives; optional MCP configuration is
  `mcp.json`.

Set a custom application root before the first setup:

```bash
export TRADE_COMPASS_HOME=/path/to/trade-compass
trade-compass setup
```

The same environment variable must be present when running the command or
installing a persistent service.

On macOS, `trade-compass service install` writes
`~/Library/LaunchAgents/com.trade-compass.serve.plist`; logs default to
`~/.trade-compass/data/logs/serve.stdout.log` and `serve.stderr.log`. On Linux,
the user unit is `$XDG_CONFIG_HOME/systemd/user/trade-compass.service` when
`XDG_CONFIG_HOME` is set, otherwise
`~/.config/systemd/user/trade-compass.service`; view logs with
`journalctl --user -u trade-compass.service -f`.

## Resolution order

The important location overrides are:

| Variable | Purpose |
| --- | --- |
| `TRADE_COMPASS_HOME` | Installed-application state root |
| `TRADE_COMPASS_CONFIG` | Explicit YAML configuration file |
| `TRADE_COMPASS_ENV_FILE` | Explicit dotenv file |
| `TRADE_COMPASS_DATA_DIR` | Runtime market and operational data |
| `TRADE_COMPASS_MEMORY_DIR` | Long-lived memory and user-created Skills |
| `TRADE_COMPASS_DATA_PROVIDER` | Selected market-data provider |
| `TRADE_COMPASS_PORT` | Web/API port |

Environment variables override their corresponding YAML values.

## LLM providers

The `llm` section selects a provider, model, API-key environment variable,
timeout, and retry count. Supported adapters include DeepSeek, OpenAI,
Anthropic, OpenRouter, DashScope, Ollama, and LM Studio.

Keep credentials in `.env`; do not place them in `config.yaml`, Skills, issues,
logs, or portable examples.

Installed-app users should normally use the setup/configure wizard instead of
editing either file directly. The files remain documented as an advanced and
automation-compatible storage format.

## Optional package features

Chart rendering used by the built-in stock-analysis agents is part of the
default installation. Extras are only needed for additional integrations and
heavier capabilities.

Add an extra to an installed tool:

```bash
uv tool install --force --python 3.12 'trade-compass-agent[tushare]'
```

Add the same extra to a source checkout:

```bash
uv sync --extra tushare
```

Confirm the resulting environment with:

```bash
trade-compass doctor
```

## Source development

Source checkouts use `config/default.yaml`. The packaged defaults are maintained
separately in `config/packaged.yaml`; changes that affect installed users should
consider both files explicitly.
