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
git tag v0.5.0
git push origin v0.5.0
```

The release workflow verifies the tag, builds once, installs the wheel in an
isolated environment, publishes through the protected `pypi` environment, and
then installs the published version from PyPI. It also runs `install.sh` against
that exact published version and verifies that configuration was not started.
GitHub Release creation happens only after those consumer checks succeed. It
attaches the installer, Python distributions, a CycloneDX SBOM, and
`SHA256SUMS`.

## Acceptance

### Installed behavior gate

CI, TestPyPI and production builds run `scripts/release_acceptance.py` with the
candidate wheel's interpreter before publication. The script verifies every
installed package file against that wheel, copies the selected consumer tests
outside the checkout, isolates all writable roots, and runs them with explicit
model/tool doubles. It covers report transport and repair, legitimate and invalid
evidence, repeated scheduled execution, persisted API/session views, native
continuation and cancellation. Source test success alone is insufficient.

The gate rejects zero tests, skips, collection errors and assertion failures.
It retains JUnit, logs, suite hashes and the exact wheel digest on failure as well
as success. Production post-publication acceptance compares the PyPI installation
to the wheel accepted before publication and reruns the same behavioral suite.
Missing evidence is not a passing result.

```bash
/path/to/installed-venv/bin/python -I scripts/release_acceptance.py \
  --wheel /path/to/candidate.whl --output /outside/checkout/new-acceptance-directory
```

Install the pinned CI pytest version into the isolated environment first. Each
output directory is exclusive: preserve failures and use a new directory after
fixing a defect. Keep the same scene and acceptance criteria when comparing a bad
release with its fix. A collection error only proves rejection; use the targeted
consumer regression separately to demonstrate the original behavioral failure.

### Real model gate

Deterministic doubles cannot establish whether a deployed model follows the wire
protocol, chooses the right metric or fits its actual context window. Run
`scripts/release_model_acceptance.py` with the exact installed wheel against the
required LM Studio model. Its fixed scene plan uses synthetic dated receipts,
real inference, isolated roots, no business writes, bounded calls and separate
processes for first/continued execution. The model and service must already be
available; the harness does not start, reconfigure or switch models.

```bash
/path/to/installed-venv/bin/python -I scripts/release_model_acceptance.py \
  --wheel /path/to/candidate.whl --model MODEL_ID \
  --output /outside/checkout/new-model-acceptance-directory
```

Availability and safety are separate requirements. Every normal scene must finish
with a complete, correct report; a safely withheld normal answer still fails the
availability gate. A deliberately missing-data scene must retain the true gap and
valid available content. The independent oracle checks dated closing-price
operands and arithmetic, not only the model's completeness verdict. Do not
reclassify an unexpected failure as an expected negative after seeing results,
drop a failed scene, increase budgets after a timeout, or retry until green.
Changing a scene or threshold requires review and a new complete evidence set.

The production `publish` job depends on `model-acceptance`. Configure repository
variables `RELEASE_MODEL_RUNNER` (an approved runner label) and `RELEASE_MODEL_ID`
(the required LM Studio model ID). With either missing, a hosted runner fails
preflight; absence of a local runner/model never skips acceptance or permits
publication. The approved runner must have LM Studio reachable on its loopback
interface and capacity reserved for acceptance. Use an isolated runner account
without production state, credentials or supervision privileges; never expose
this runner to pull-request jobs. The harness does not install/register a runner
or change repository variables. Until that infrastructure is approved and ready,
production releases are intentionally blocked. TestPyPI remains a deterministic
artifact preview, not permission to deploy to the stable service.

Other providers need their own real-adapter acceptance; native protocol doubles
do not certify vendor authentication or real inference. A model profile change
requires a new complete fixed-plan evidence set. The fixed small suite is a
regression gate, not a statistical guarantee of future model reliability.

These gates cover the report incident class, not all product journeys. Expand
consumer contracts with affected features: first/continued use, restart, upgrade
and rollback with new records, real UI drafts and device authorization where
applicable. Deployments still require a stopped-state backup, one production
writer and an authorized read-only probe. Do not auto-restore older state after
new trades or replay mutations to test a release. Define candidate/production
failure criteria before cutover and retain the prior program for compatible
program-only recovery.

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
uv tool install --python 3.12 'trade-compass-agent==0.5.0'
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
