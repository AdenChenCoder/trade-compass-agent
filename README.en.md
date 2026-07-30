# Trade Compass Agent

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-005571?logo=fastapi)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=111)](https://react.dev/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/AdenChenCoder/trade-compass-agent/blob/main/LICENSE)
[![PyPI](https://img.shields.io/pypi/v/trade-compass-agent)](https://pypi.org/project/trade-compass-agent/)
[![CI](https://github.com/AdenChenCoder/trade-compass-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/AdenChenCoder/trade-compass-agent/actions/workflows/ci.yml)

[简体中文](README.md) | [English](README.en.md)

**A local-first AI research workbench built for the A-share market.**

Trade Compass Agent combines market data, technical and fundamental analysis, specialist agents, paper portfolios, scheduled workflows, and long-term memory in one Web and CLI application. Use it to research stocks, build trading plans, track signals, review decisions, and turn your own research process into a repeatable workflow.

![Trade Compass Web workbench](https://raw.githubusercontent.com/AdenChenCoder/trade-compass-agent/main/docs/assets/trade-compass-workbench.png)

## Quick start

### Requirements

- macOS or Linux
- An API key for a remote model provider, or a local Ollama / LM Studio service

The recommended installer bootstraps a pinned
[uv](https://docs.astral.sh/uv/) version when needed, then installs the stable
package in an isolated tool environment:

```bash
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/AdenChenCoder/trade-compass-agent/releases/latest/download/install.sh | sh
```

If uv is already installed, install directly from the
[stable PyPI package](https://pypi.org/project/trade-compass-agent/):

```bash
uv tool install --python 3.12 trade-compass-agent
```

Then initialize and start the application:

```bash
trade-compass setup
trade-compass doctor
trade-compass serve --open
```

The first setup creates local configuration and guides you through choosing a
model and market-data provider. Use `trade-compass configure` later to make
changes. The installer does not start setup or modify existing configuration.
See [Getting started](docs/getting-started.md) and
[Configuration](docs/configuration.md) for the full reference.

Open `http://127.0.0.1:19704/agent` and start a conversation.

```text
Analyze 600519 using price action, fundamentals, recent disclosures,
and market context. Give me the key drivers, risks, and a trading plan.
```

## Features

### AI-powered market research

The agent can retrieve market data, calculate technical indicators, inspect fundamentals and company disclosures, search news and research sources, analyze fund flows, and combine the results into one response.

### Specialist agent team

Dispatch focused research to built-in specialists:

- **Intraday Technical** — short-horizon price structure and technical signals
- **Equity Research** — fundamentals, research, bull/bear debate, and PM synthesis
- **Macro Sentiment** — macro conditions, market sentiment, and fund flows
- **Screener** — AI review of quantitative screening candidates
- **Chokepoint Analyst** — supply-chain bottlenecks and critical upstream companies
- **Risk Advisor** — exposure, concentration, drawdown, and portfolio risk

### A-share market toolkit

- Daily and intraday bars with pluggable data providers
- Technical indicators including MA, MACD, RSI, Bollinger Bands, and ATR
- Market pulse, sector strength, fund flow, fundamentals, announcements, and news
- A-share lot sizes, T+0/T+1 rules, board-specific price limits, fees, and slippage
- AkShare and Baostock included; optional Tushare and CNInfo support

### Paper portfolio and signal tracking

Create multiple paper accounts, record simulated trades, analyze positions and P&L, check portfolio concentration, and evaluate signal follow-through after 1, 3, and 5 trading days.

### Automated research workflows

Run complete research workflows on demand or on a schedule:

- Premarket briefing and morning plan
- Stock screening and idea generation
- Intraday technical and equity research
- Catalyst calendar and close check
- End-of-day and weekly review

### Extensible Skills

Built-in Skills cover catalyst calendars, idea generation, intraday technical
analysis, and investment-master methods. You can add your own Skills to make
personal research processes and output standards repeatable.

### Memory, rules, and review

Trade Compass stores sessions, research notes, user rules, decisions, and reviews locally. Memory search, reflection, contradiction detection, and decision reconciliation help carry useful context into future research.

### Web, CLI, API, and integrations

Use the React workbench, automate with the CLI, build on the FastAPI API, connect MCP tools, and deliver notifications through Feishu, WeCom, Weixin, or generic Webhooks.

## How it works

```mermaid
flowchart LR
    U["Web / CLI / API"] --> A["Trade Compass Agent"]
    A --> T["Market & research tools"]
    A --> S["Specialist agents"]
    T --> P["Trading plan & signals"]
    S --> P
    P --> F["Paper portfolio"]
    F --> R["Review & memory"]
    J["Scheduled workflows"] --> A
```

## Usage

### Web workbench

```bash
trade-compass serve
```

The Web UI includes agent chat, session history, paper portfolios, memory, audit records, user rules, skills, scheduled jobs, and settings. The interactive API reference is available at `http://127.0.0.1:19704/docs`.

Start without scheduled background jobs:

```bash
trade-compass serve --no-scheduler
```

### CLI

```bash
# Ask the agent
trade-compass agent "How is the A-share market today?"

# Inspect market data
trade-compass market-pulse
trade-compass data check 600519 510300

# Work with schedules, rules, and research history
trade-compass jobs list
trade-compass rules list
trade-compass audit recent --limit 20
trade-compass evaluate --limit 100
```

Run `trade-compass --help` to see all commands.

### Run as a local service

```bash
trade-compass service install
trade-compass service status
trade-compass service verify
```

## Configuration

Manage day-to-day settings with the configuration wizard:

```bash
trade-compass configure
```

The stable package and writable user state stay separate. Use these commands to
show the actual uv paths on the current machine:

```bash
uv tool list --show-paths
uv tool dir
uv tool dir --bin
```

| Content | Default location or lookup |
| --- | --- |
| `trade-compass` command | `uv tool dir --bin` |
| Isolated package environment | `uv tool dir` / `uv tool list --show-paths` |
| Application home | `~/.trade-compass/`, overridable with `TRADE_COMPASS_HOME` |
| Configuration and secrets | `~/.trade-compass/config.yaml`, `~/.trade-compass/.env` |
| Data and memory | `~/.trade-compass/data/`, `~/.trade-compass/memory_vault/` |
| Backups | `~/.trade-compass/backups/` |

Secrets must not be added to configuration files or committed to Git. See
[Configuration](docs/configuration.md) for path overrides, service definitions,
and log locations.

Supported LLM providers include DeepSeek, OpenAI, Anthropic, OpenRouter, DashScope, Ollama, and LM Studio. The default dependencies include chart rendering for stock analysis. Optional extras add Tushare, MCP clients, messaging channels, forecasting, and enhanced search.

Example: enable Tushare data.

```bash
uv tool install --force --python 3.12 'trade-compass-agent[tushare]'
```

Run `trade-compass configure`, select the automatic or Tushare data
provider, and enter the token.

## Documentation

| Goal | Guide |
| --- | --- |
| Install and complete the first run | [Getting started](https://github.com/AdenChenCoder/trade-compass-agent/blob/main/docs/getting-started.md) |
| Configure providers, storage, and optional features | [Configuration](https://github.com/AdenChenCoder/trade-compass-agent/blob/main/docs/configuration.md) |
| Use and automate the command line | [CLI reference](https://github.com/AdenChenCoder/trade-compass-agent/blob/main/docs/cli.md) |
| Create and package runtime Skills | [Skills](https://github.com/AdenChenCoder/trade-compass-agent/blob/main/docs/skills.md) |
| Understand repository and state boundaries | [Architecture](https://github.com/AdenChenCoder/trade-compass-agent/blob/main/docs/architecture.md) |
| Understand local data and external-service boundaries | [Privacy](https://github.com/AdenChenCoder/trade-compass-agent/blob/main/PRIVACY.md) / [Threat model](https://github.com/AdenChenCoder/trade-compass-agent/blob/main/docs/threat-model.md) |
| Build, verify, and publish a release | [Releasing](https://github.com/AdenChenCoder/trade-compass-agent/blob/main/docs/releasing.md) |

## Development

```bash
uv sync --extra dev
pnpm install --frozen-lockfile

uv run trade-compass serve --dev  # API on :19704
pnpm --dir apps/web dev            # Web UI on :3000
```

Run the project checks before opening a pull request:

```bash
scripts/ci_check.sh
pnpm --dir apps/web test
pnpm --dir apps/web typecheck
pnpm --dir apps/web build
git diff --check
```

## Community

- Read [CONTRIBUTING.md](https://github.com/AdenChenCoder/trade-compass-agent/blob/main/CONTRIBUTING.md) before proposing a substantial change.
- Use [SUPPORT.md](https://github.com/AdenChenCoder/trade-compass-agent/blob/main/SUPPORT.md) to choose the right support channel.
- Report vulnerabilities through [SECURITY.md](https://github.com/AdenChenCoder/trade-compass-agent/blob/main/SECURITY.md), not a public issue.
- See [PRIVACY.md](https://github.com/AdenChenCoder/trade-compass-agent/blob/main/PRIVACY.md) for data-flow and retention boundaries.
- Participation is governed by [CODE_OF_CONDUCT.md](https://github.com/AdenChenCoder/trade-compass-agent/blob/main/CODE_OF_CONDUCT.md).
- Release history is maintained in [CHANGELOG.md](https://github.com/AdenChenCoder/trade-compass-agent/blob/main/CHANGELOG.md).

## License

[MIT](https://github.com/AdenChenCoder/trade-compass-agent/blob/main/LICENSE)
