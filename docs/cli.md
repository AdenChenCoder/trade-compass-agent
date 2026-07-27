# CLI reference

The command line is organized around user journeys and resource/action pairs:

```text
trade-compass <resource> <action>
```

Run `trade-compass commands` for the canonical catalog or
`trade-compass commands --json` for machine-readable metadata. The same catalog
is available from `GET /api/agent/commands`.

## First-run commands

```bash
trade-compass setup
trade-compass configure
trade-compass doctor
trade-compass serve --open
trade-compass agent "今天 A 股市场怎么样？"
```

`setup` launches the guided wizard for installed applications. `configure` is
an alias for rerunning it. Use `setup --non-interactive` for template-only
automation, or `setup --wizard` to opt into the wizard from a source checkout.

## Research and automation

```bash
trade-compass market-pulse
trade-compass data check 600519 510300
trade-compass jobs list
trade-compass jobs run close
trade-compass audit recent --limit 20
trade-compass evaluate --limit 100
```

## Memory

```bash
trade-compass memory reindex
trade-compass memory bootstrap --dry-run
trade-compass memory contradictions
```

## Recovery and services

```bash
trade-compass backup create
trade-compass backup inspect path/to/backup.zip
trade-compass restore path/to/backup.zip
trade-compass service status --json
trade-compass service verify --json
```

Restore and import commands preview changes unless `--force` is supplied.

## Compatibility aliases

Earlier flat commands such as `data-check`, `run-job`, `memory-reindex`, and
the `scheduler` group remain accepted. New documentation uses canonical grouped
commands. Scripts can discover aliases through `trade-compass commands --json`.

Shell commands and Web chat commands are different surfaces. In Web chat,
`/skill <name>` selects an application Skill; it does not invoke a shell
command.
