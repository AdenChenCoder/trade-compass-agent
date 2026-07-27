# Getting started

## Install

Trade Compass requires Python 3.12 or newer. The released wheel contains the
production Web UI, so Node.js is not required for normal use.

```bash
uv tool install trade-compass-agent
trade-compass setup
```

`setup` creates the installed-application home at `~/.trade-compass/` by
default and starts a guided terminal wizard. It configures the model credential,
storage, market data, automation, optional channels and search, and privacy
without asking installed-app users to edit files. To use another location,
export `TRADE_COMPASS_HOME` before running setup.

## Configure an LLM

Choose the provider and model in the setup wizard. Remote-provider credentials
are entered through a masked prompt and saved to the protected local env file.
Run `trade-compass configure` whenever you need to change the selection or key.

Run the readiness check:

```bash
trade-compass doctor
```

The command reports each configuration or runtime dependency that still needs
attention.

For scripts or image builds, `trade-compass setup --non-interactive` retains the
template-only initialization behavior. Source checkouts also use that behavior
by default; pass `--wizard` to opt into guided configuration there.

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
