# Releasing

Releases publish one verified set of Python artifacts to PyPI and attach the
same files to a GitHub Release.

Before the repository's first public release, complete the
[public repository release checklist](public-release-checklist.md).

## Preconditions

- The version in `pyproject.toml` and `trade_compass_agent.__version__` agrees.
- `CHANGELOG.md` describes user-visible changes.
- CI, Web tests, type checking, build checks, and security scanning pass.
- The GitHub `pypi` and `testpypi` environments are configured.
- PyPI Trusted Publishers match the repository, workflow, and environment.

## TestPyPI

Run the `TestPyPI Publish` workflow manually. It:

1. verifies source and Web code;
2. builds wheel and sdist once;
3. checks package contents;
4. publishes through OIDC;
5. downloads the exact uploaded wheel without using TestPyPI as a dependency
   index;
6. installs dependencies from production PyPI and runs the installed-package
   smoke test.

## Production

Create and push a tag that exactly matches the package version:

```bash
git tag v0.3.0
git push origin v0.3.0
```

The release workflow verifies the tag, builds once, installs the wheel in an
isolated environment, publishes through the protected `pypi` environment, and
then installs the published version from PyPI. It also runs `install.sh` against
that exact published version and verifies that configuration was not started.
GitHub Release creation happens only after those consumer checks succeed. It
attaches the installer, Python distributions, a CycloneDX SBOM, and
`SHA256SUMS`.

## Acceptance

The installed-package smoke test verifies:

- the importable version;
- packaged Web UI, schemas, workflows, specialists, defaults, and Skills;
- YAML Skill metadata and bundled references;
- writable runtime paths outside the package;
- the console entry point and command catalog.
- the one-command installer, including its no-automatic-setup contract.
- the release checksums and dependency SBOM.

If publication succeeds but post-publish acceptance fails, do not reuse the
version number. Diagnose the artifact, publish a new patch or release candidate,
and document the failure in the release notes.

## Rollback

PyPI files and versions are immutable. A bad release can be yanked to discourage
new installations, but consumers may still possess it. Restore service by
publishing a corrected version and keeping the affected release notes explicit.

## Production installation and upgrade

Source checkouts are development and validation environments. Keep their ports,
data, memory and channel/mobile credentials separate from the permanent service.
Do not stop production to run a development server or install a source checkout
as the production service.

Only switch the service after the reviewed commit is pushed, the tagged release
is published, and the published version passes installed-package acceptance
outside the repository. Pin the version and preserve the extras already in use:

```bash
uv tool install --python 3.12 'trade-compass-agent==0.3.0'
```

For an existing tool installation use `--force`; append the existing optional
extras to the package name. Accept the new version in a separate environment
first, then stop the service before replacing its installed tool environment.
Installation does not restart the process. Use the existing supervisor to start
the service at the agreed cutover.

Before switching, wait for active work to finish and make a consistent backup of
the configuration, environment, complete data and memory roots, and supervisor
definition. Keep the backup outside the checkout and the active state directory.
The installed default root is `~/.trade-compass`; retain model choices, account
records, history, human rules, learned Skills, channel credentials and the full
mobile state. Never run two copies of the same mobile identity concurrently.
When moving roots, update structured artifact and attachment path references;
preserve historical prose and stable record IDs.

Verify the service imports the published package, uses the intended writable
roots, serves its packaged UI, retains historical results and balances, and
restores the same mobile origin and pairing after restart. Preserve the previous
program and matching state backup until acceptance. A rollback must account for
any new writes; never replay trades or overwrite newer records without review.
