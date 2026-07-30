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

Python and JavaScript dependencies are not vendored into the source tree. Their
licenses remain governed by their respective distributions.
