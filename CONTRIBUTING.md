# Contributing

Thanks for helping improve Trade Compass Agent.

## Choose the right change

- Reproduce bugs before changing implementation.
- Open a feature request before a large product, architecture, persistence, or
  workflow change.
- Add repeatable procedural guidance as a Skill, deterministic capability as a
  runtime tool, and focused reasoning roles as specialist assets.
- Keep pull requests focused. Refactor-only work should identify the observable
  maintenance or reliability outcome it enables.

The repository map and extension boundaries are described in
[docs/architecture.md](docs/architecture.md).

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

When adding or changing a built-in Skill, follow [docs/skills.md](docs/skills.md)
and verify that the Skill is available from both a source checkout and an
installed wheel.

## Verify a TestPyPI release

Never combine TestPyPI and production PyPI as competing dependency indexes.
Download only the project wheel from TestPyPI, then install that local wheel
with dependencies resolved exclusively from production PyPI:

```bash
VERSION=0.2.0rc3
DOWNLOAD_DIR="$(mktemp -d)"

python3.12 -m venv .venv-release
source .venv-release/bin/activate
python -m pip download \
  --index-url https://test.pypi.org/simple/ \
  --no-deps \
  --only-binary=:all: \
  --dest "$DOWNLOAD_DIR" \
  "trade-compass-agent==$VERSION"
python -m pip install \
  --index-url https://pypi.org/simple/ \
  "$DOWNLOAD_DIR/trade_compass_agent-$VERSION-py3-none-any.whl"

trade-compass --version
python -m pip check
```

By contributing, you agree that your contribution is licensed under the repository's MIT License.
