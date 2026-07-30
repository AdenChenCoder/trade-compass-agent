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
git tag v0.2.0
git push origin v0.2.0
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
