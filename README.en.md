# Trade Compass Agent

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-005571?logo=fastapi)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=111)](https://react.dev/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/AdenChenCoder/trade-compass-agent/blob/main/LICENSE)
[![CI](https://github.com/AdenChenCoder/trade-compass-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/AdenChenCoder/trade-compass-agent/actions/workflows/ci.yml)

[简体中文](README.md) | [English](README.en.md)

**A local-first AI research workbench built for the A-share market.**

Trade Compass Agent combines market data, technical and fundamental analysis, specialist agents, paper portfolios, scheduled workflows, and long-term memory in one Web and CLI application. Use it to research stocks, build trading plans, track signals, review decisions, and turn your own research process into a repeatable workflow.

![Trade Compass Web workbench](https://raw.githubusercontent.com/AdenChenCoder/trade-compass-agent/main/docs/assets/trade-compass-workbench.png)

## Quick start

### Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Node.js 22 and pnpm 11.1.3
- An API key for a remote model provider, or a local Ollama / LM Studio service

The project is currently in pre-release and does not yet publish a PyPI package
or GitHub Release. Run it from source for now:

```bash
git clone https://github.com/AdenChenCoder/trade-compass-agent.git
cd trade-compass-agent
uv sync
pnpm install --frozen-lockfile
pnpm --dir apps/web build
uv run trade-compass setup --wizard
uv run trade-compass doctor
uv run trade-compass serve --open
```

The first run creates local configuration and guides you through choosing a
model and market-data provider. Use `uv run trade-compass configure` later to
make changes. See [Getting started](docs/getting-started.md) and
[Configuration](docs/configuration.md) for the full setup reference.

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
uv run trade-compass serve
```

The Web UI includes agent chat, session history, paper portfolios, memory, audit records, user rules, skills, scheduled jobs, and settings. The interactive API reference is available at `http://127.0.0.1:19704/docs`.

Start without scheduled background jobs:

```bash
uv run trade-compass serve --no-scheduler
```

### CLI

```bash
# Ask the agent
uv run trade-compass agent "How is the A-share market today?"

# Inspect market data
uv run trade-compass market-pulse
uv run trade-compass data check 600519 510300

# Work with schedules, rules, and research history
uv run trade-compass jobs list
uv run trade-compass rules list
uv run trade-compass audit recent --limit 20
uv run trade-compass evaluate --limit 100
```

Run `uv run trade-compass --help` to see all commands.

### Run as a local service

```bash
uv run trade-compass service install
uv run trade-compass service status
uv run trade-compass service verify
```

## Configuration

Manage day-to-day settings with the configuration wizard:

```bash
uv run trade-compass configure
```

Source checkouts use `config/default.yaml` and the repository `.env`; secrets
must not be added to configuration files or committed to Git. Installed
application settings and credentials stay locally under `~/.trade-compass/`.

Supported LLM providers include DeepSeek, OpenAI, Anthropic, OpenRouter, DashScope, Ollama, and LM Studio. The default dependencies include chart rendering for stock analysis. Optional extras add Tushare, MCP clients, messaging channels, forecasting, and enhanced search.

Example: enable Tushare data.

```bash
uv sync --extra tushare
```

Run `uv run trade-compass configure`, select the automatic or Tushare data
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
