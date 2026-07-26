# Getting started

## Install

Trade Compass requires Python 3.12 or newer. The released wheel contains the
production Web UI, so Node.js is not required for normal use.

```bash
uv tool install trade-compass-agent
trade-compass setup
```

`setup` creates the installed-application home at `~/.trade-compass/` by
default. To use another location, export `TRADE_COMPASS_HOME` before running
setup.

## Configure an LLM

Add the credential expected by your configured provider to
`~/.trade-compass/.env`. DeepSeek is the packaged default:

```dotenv
DEEPSEEK_API_KEY=replace-me
```

Run the readiness check:

```bash
trade-compass doctor
```

The command reports each configuration or runtime dependency that still needs
attention.

## Start the workbench

```bash
trade-compass serve --open
```

If a browser is not opened automatically, visit
`http://127.0.0.1:19704/agent`.

Try a first request:

```text
结合价格结构、基本面、近期公告和市场环境分析 600519，
给出核心驱动因素、主要风险和交易计划。
```

## Next steps

- Configure providers and storage in [Configuration](configuration.md).
- Learn the stable automation surface in [CLI reference](cli.md).
- Extend the agent with [Skills](skills.md).
