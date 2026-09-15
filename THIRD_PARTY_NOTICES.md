# Third-party notices

Trade Compass Agent includes source derived from the following project. Its
license is distributed with the relevant package files.

## Kronos

- Project: `shiyu-coder/Kronos`
- Upstream: https://github.com/shiyu-coder/Kronos
- Upstream revision: `67b630e67f6a18c9e9be918d9b4337c960db1e9a`
- License: MIT
- Vendored path: `src/trade_compass_agent/data/kronos/`
- License text: `src/trade_compass_agent/data/kronos/LICENSE`
- Local change: package-local relative imports replace the upstream
  `sys.path` mutation; `module.py`, `__init__.py`, and the license text match
  the recorded revision.

The optional forecasting feature downloads the following MIT-licensed model
and tokenizer weights from Hugging Face at runtime; they are not bundled in
this repository or its Python distributions:

- `NeoQuasar/Kronos-mini`
- `NeoQuasar/Kronos-small`
- `NeoQuasar/Kronos-base`
- `NeoQuasar/Kronos-Tokenizer-2k`
- `NeoQuasar/Kronos-Tokenizer-base`

Their model cards and current license metadata remain authoritative for those
separate downloads: https://huggingface.co/NeoQuasar

## Mobile connection component

The Python distribution includes compiled mobile connection components built
from `scripts/mobile-funnel-probe/`. They embed `tailscale.com` v1.102.4
(https://github.com/tailscale/tailscale/tree/v1.102.4, BSD-3-Clause), its imported
dependencies, and the Go runtime. Dependency versions are pinned in that
directory's `go.mod` and `go.sum`.

The build collects dependency license and notice files for all four target
platforms, together with the Go runtime license, in the packaged
`trade_compass_agent/mobile_bin/LICENSES.txt`. The component manifest records
the checksum of this notice file as well as the distributed binaries.

Python and JavaScript dependencies are not vendored into the source tree. Their
licenses remain governed by their respective distributions.
