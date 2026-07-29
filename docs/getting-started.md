# Getting started

## Install

Trade Compass runs on Python 3.12 or newer; the one-command installer can
provision that runtime through `uv`. The released wheel contains the production
Web UI and the chart-rendering dependencies used by stock analysis, so Node.js
or an additional chart package is not required for normal use.

On macOS or Linux, install the latest production release with:

```bash
curl --proto '=https' --tlsv1.2 -fsSL \
  https://github.com/AdenChenCoder/trade-compass-agent/releases/latest/download/install.sh | sh
```

The installer bootstraps `uv` when needed and prints the command path and next
steps. It does not run `setup` or create application configuration.

Alternatively, install manually with an existing `uv`:

```bash
uv tool install trade-compass-agent
```

When you are ready to configure the application:

```bash
trade-compass setup
```

`setup` creates the installed-application home at `~/.trade-compass/` by
default and starts a guided terminal wizard. It configures the model credential,
storage, market data, automation, optional channels and search, and privacy
without asking installed-app users to edit files. To use another location,
export `TRADE_COMPASS_HOME` before running setup.
Use `↑/↓` to move between choices, `Space` to toggle multi-select items, and
`Enter` to confirm. Secret values are masked while typing, and each answer
advances to the next configuration item.

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
