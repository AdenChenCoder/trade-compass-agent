# Trade Compass Web (`apps/web`)

Vite 6 + React 19 SPA for the Agent-first UI.

## Scripts

```bash
pnpm --dir apps/web dev        # http://127.0.0.1:3000, proxies /api → :19704
pnpm --dir apps/web build      # output: src/trade_compass_agent/web_dist/
pnpm --dir apps/web typecheck
```

## Routes

- `/agent` — chat + SSE (`/api/agent/stream`) + turn (`POST /api/agent/turn`)
- `/settings` — read-only skills/MCP from `/api/agent/skills` and `/api/agent/mcp`

The wheel build copies `web_dist` via `hatch_build.py` when packaging.
