# Trade Compass Web (`apps/web`)

Vite 6 + React 19 SPA for the Agent-first UI.

## Scripts

```bash
uv run trade-compass dev       # from repository root: API + Web UI with hot reload
pnpm --dir apps/web dev        # http://127.0.0.1:3000, proxies /api → :19704
pnpm --dir apps/web build      # output: src/trade_compass_agent/web_dist/
pnpm --dir apps/web build:preview # atomic preview update: temp/web-preview/
pnpm --dir apps/web typecheck
```

## Routes

- `/agent` — chat + SSE (`/api/agent/stream`) + turn (`POST /api/agent/turn`)
- `/settings` — read-only skills/MCP from `/api/agent/skills` and `/api/agent/mcp`

The wheel build copies `web_dist` via `hatch_build.py` when packaging.

For an isolated Python preview that stays open while the UI changes, build with
`build:preview` and start that preview with `TRADE_COMPASS_WEB_DIST_OVERRIDE` set
to the absolute repository `temp/web-preview` path. Keep its configuration, data,
memory and port separate from the stable service. This publishes assets before
switching the HTML entry and retains hashed chunks needed by existing tabs.
Release builds remain clean and do not overwrite this preview directory.
Remove the preview directory only after stopping its server and closing its tabs;
the next preview build recreates it.
