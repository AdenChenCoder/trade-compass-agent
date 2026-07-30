# Security audit exceptions

Security audit exceptions are narrow, documented, and reviewed whenever the
affected dependency or application architecture changes.

## GHSA-qwww-vcr4-c8h2 — React Router RSC mode

- **Dependency:** `react-router` 7.18.1, through `react-router-dom`
- **Scope:** the advisory affects React Router's experimental RSC action
  handling.
- **Why it is not reachable here:** the bundled Web UI is a client-rendered
  Vite SPA using `createBrowserRouter`. It does not use React Server
  Components, React Router's RSC plugin, server actions, or an RSC server
  runtime.
- **Control:** `pnpm-workspace.yaml` ignores only `GHSA-qwww-vcr4-c8h2`; CI
  runs the normal `pnpm audit`, so every other npm advisory remains blocking.
- **Revisit when:** RSC, SSR, React Router actions, or an RSC-capable build
  plugin is introduced, or a stable patched React Router release is available.

Last reviewed: 2026-07-30.
