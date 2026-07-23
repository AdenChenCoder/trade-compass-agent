# Contributing

Thanks for helping improve Trade Compass Agent.

## Development setup

Python 3.12+ and Node.js 20+ with pnpm 9 are required.

```bash
uv pip install -e ".[dev]"
pnpm install
cp .env.example .env
```

Run the API and web development server in separate terminals:

```bash
trade-compass serve --dev
pnpm --dir apps/web dev
```

## Before opening a pull request

```bash
scripts/ci_check.sh
pnpm --dir apps/web test
pnpm --dir apps/web typecheck
pnpm --dir apps/web build
git diff --check
```

Keep changes focused, add tests for observable behavior, and do not commit API keys, local `.env` files, generated runtime data, or broker credentials. Describe user-visible behavior, compatibility impact, and rollback steps in the pull request.

By contributing, you agree that your contribution is licensed under the repository's MIT License.
