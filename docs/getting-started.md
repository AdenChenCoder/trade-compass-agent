# Getting started

## Run from source

Trade Compass is currently in pre-release and does not yet publish a PyPI
package or GitHub Release. The current setup path requires Python 3.12 or
newer, `uv`, Node.js 22, and pnpm 11.1.3.

```bash
git clone https://github.com/AdenChenCoder/trade-compass-agent.git
cd trade-compass-agent
uv sync
pnpm install --frozen-lockfile
pnpm --dir apps/web build
uv run trade-compass setup --wizard
```

`setup --wizard` creates the source-checkout `.env` when needed and starts a
guided terminal wizard. It configures the model credential, storage, market
data, automation, optional channels and search, and privacy.
Use `↑/↓` to move between choices, `Space` to toggle multi-select items, and
`Enter` to confirm. Secret values are masked while typing, and each answer
advances to the next configuration item.

## Configure an LLM

Choose the provider and model in the setup wizard. Remote-provider credentials
are entered through a masked prompt and saved to the protected local env file.
Run `uv run trade-compass configure` whenever you need to change the selection
or key.

Run the readiness check:

```bash
uv run trade-compass doctor
```

The command reports each configuration or runtime dependency that still needs
attention.

For scripts or image builds, `uv run trade-compass setup --non-interactive`
initializes templates and runtime directories without starting the wizard.

## Start the workbench

```bash
uv run trade-compass serve --open
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
