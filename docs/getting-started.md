# Getting started

## Install the stable package

Trade Compass supports macOS and Linux. The recommended installer bootstraps a
pinned `uv` version when needed and installs the exact package version attached
to the latest GitHub Release:

```bash
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/AdenChenCoder/trade-compass-agent/releases/latest/download/install.sh | sh
```

Release assets include `SHA256SUMS` for users who prefer to download and verify
the installer before running it.

If `uv` is already installed, install directly from PyPI:

```bash
uv tool install --python 3.12 trade-compass-agent
```

Both methods create an isolated tool environment; neither starts setup or
changes an existing `~/.trade-compass` directory.

## Complete the first run

```bash
trade-compass setup
trade-compass doctor
trade-compass serve --open
```

`setup` creates the installed-application files and starts a guided terminal
wizard. It configures the model credential, storage, market data, automation,
optional channels and search, and privacy. Secret values are masked while
typing.

Run `trade-compass configure` whenever you need to change the selection or key.
For scripts or image builds, `trade-compass setup --non-interactive`
initializes templates and runtime directories without starting the wizard.

## Run from source

Contributors need Python 3.12 or newer, `uv`, Node.js 22, and pnpm 11.1.3:

```bash
git clone https://github.com/AdenChenCoder/trade-compass-agent.git
cd trade-compass-agent
uv sync
pnpm install --frozen-lockfile
pnpm --dir apps/web build
uv run trade-compass setup --wizard
```

Use the `uv run trade-compass ...` form for the remaining commands when working
from this source checkout.

## Configure an LLM

Choose the provider and model in the setup wizard. Remote-provider credentials
are entered through a masked prompt and saved to the protected local env file.

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
