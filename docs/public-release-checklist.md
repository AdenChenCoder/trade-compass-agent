# Public repository release checklist

Use this checklist immediately before changing the GitHub repository from
private to public. Repository settings and history changes are intentionally
not automated by the source tree.

## Identity and history

- Review every author and committer email in the complete Git history. If an
  address should not become public, rewrite it **before** publication and have
  every collaborator replace old clones.
- Run Gitleaks against the complete history and the final working tree.
- Review GitHub Actions logs and uploaded artifacts for credentials, local
  paths, private datasets, and account identifiers.
- Rotate any credential that was ever committed or printed, even if it was
  later removed.

## GitHub security settings

- Enable private vulnerability reporting.
- Enable Dependabot alerts and security updates.
- Enable secret scanning and push protection.
- Enable code scanning after the repository is public, then make the result a
  required check.
- Create a `main` ruleset requiring pull requests, resolved conversations, and
  successful `CI` and `Security` workflows. Restrict force pushes and branch
  deletion.
- Keep Actions default workflow permissions read-only and allow only required
  actions. Full commit SHAs are already enforced in the workflow files.

## Publishing

- Create protected `testpypi` and `pypi` environments. Limit deployment
  branches/tags and require a reviewer for production.
- Verify PyPI Trusted Publisher subjects exactly match this repository,
  workflow filename, and environment.
- Run `TestPyPI Publish`; install the exact uploaded wheel while resolving
  dependencies only from production PyPI.
- Confirm `SHA256SUMS`, `sbom.cdx.json`, wheel, sdist, and `install.sh` are
  attached to the GitHub Release and that the checksums verify.
- Never reuse a PyPI version after a failed or partial publication.

## Public-facing review

- Confirm [PRIVACY.md](../PRIVACY.md), [SECURITY.md](../SECURITY.md),
  [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md), and the
  [threat model](threat-model.md) match the release.
- Reconfirm the recorded Kronos source revision and current model-card license
  metadata.
- Verify the README installation path from a clean machine or container.
- Verify issues, discussions, support links, vulnerability reporting, and
  release links work for a signed-out visitor.

## Rollback boundary

Changing a public repository back to private does not retract clones, forks,
caches, package releases, or indexed history. Treat the visibility change as
irreversible disclosure and complete this checklist first.
