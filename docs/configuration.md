# Configuration

Trade Compass keeps installed-application state separate from package files.

```text
~/.trade-compass/
├── config.yaml
├── .env
├── data/
└── memory_vault/
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

Add an extra to a source checkout:

```bash
uv sync --extra tushare
```

Confirm the resulting environment with:

```bash
uv run trade-compass doctor
```

## Source development

Source checkouts use `config/default.yaml`. The packaged defaults are maintained
separately in `config/packaged.yaml`; changes that affect installed users should
consider both files explicitly.
