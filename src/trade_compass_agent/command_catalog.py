"""User-facing command metadata shared by CLI help, APIs, and documentation."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CommandSpec:
    path: tuple[str, ...]
    summary: str
    category: str
    aliases: tuple[tuple[str, ...], ...] = ()
    mutates_state: bool = False
    supports_json: bool = False

    @property
    def command(self) -> str:
        return "trade-compass " + " ".join(self.path)

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["path"] = list(self.path)
        payload["aliases"] = [list(alias) for alias in self.aliases]
        payload["command"] = self.command
        return payload


COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        ("setup",),
        "Create local config, env, and runtime directories.",
        "onboarding",
        mutates_state=True,
    ),
    CommandSpec(
        ("doctor",), "Check configuration, storage, UI, LLM, and service readiness.", "onboarding"
    ),
    CommandSpec(
        ("serve",), "Start the local Web workbench and API.", "onboarding", mutates_state=True
    ),
    CommandSpec(("agent",), "Run one agent turn with tools.", "research", aliases=(("ask",),)),
    CommandSpec(("market-pulse",), "Show sector strength and limit-up activity.", "research"),
    CommandSpec(
        ("data", "check"),
        "Diagnose configured market-data providers.",
        "research",
        aliases=(("data-check",),),
    ),
    CommandSpec(
        ("jobs", "list"),
        "List built-in and custom scheduled jobs.",
        "automation",
        aliases=(("scheduler", "list"),),
    ),
    CommandSpec(
        ("jobs", "start"),
        "Run the foreground scheduler.",
        "automation",
        aliases=(("scheduler", "start"),),
        mutates_state=True,
    ),
    CommandSpec(
        ("jobs", "runs"),
        "Show recent scheduled-job runs.",
        "automation",
        aliases=(("scheduler", "runs"),),
    ),
    CommandSpec(
        ("jobs", "run"),
        "Run one scheduled job immediately.",
        "automation",
        aliases=(("run-job",), ("scheduler", "run-once")),
        mutates_state=True,
    ),
    CommandSpec(
        ("jobs", "add"),
        "Create a custom prompt job.",
        "automation",
        aliases=(("scheduler", "add"),),
        mutates_state=True,
    ),
    CommandSpec(
        ("jobs", "pause"),
        "Pause a custom job.",
        "automation",
        aliases=(("scheduler", "pause"),),
        mutates_state=True,
    ),
    CommandSpec(
        ("jobs", "resume"),
        "Resume a custom job.",
        "automation",
        aliases=(("scheduler", "resume"),),
        mutates_state=True,
    ),
    CommandSpec(
        ("jobs", "remove"),
        "Delete a custom job.",
        "automation",
        aliases=(("scheduler", "remove"),),
        mutates_state=True,
    ),
    CommandSpec(("notifications", "recent"), "Show recent local notifications.", "automation"),
    CommandSpec(
        ("notifications", "test"),
        "Send a local test notification.",
        "automation",
        mutates_state=True,
    ),
    CommandSpec(("rules", "list"), "List human-owned rules.", "memory"),
    CommandSpec(("rules", "show"), "Print active human-owned rules.", "memory"),
    CommandSpec(("rules", "add"), "Add a human-owned rule.", "memory", mutates_state=True),
    CommandSpec(("rules", "edit"), "Edit a human-owned rule.", "memory", mutates_state=True),
    CommandSpec(("rules", "remove"), "Remove a human-owned rule.", "memory", mutates_state=True),
    CommandSpec(
        ("memory", "merge"),
        "Merge similar knowledge entries with the configured LLM.",
        "memory",
        aliases=(("memory-merge",),),
        mutates_state=True,
    ),
    CommandSpec(
        ("memory", "pin"),
        "Pin a high-trust knowledge or user entry.",
        "memory",
        aliases=(("memory-pin",),),
        mutates_state=True,
    ),
    CommandSpec(
        ("memory", "forget"),
        "Forget an entry by text prefix.",
        "memory",
        aliases=(("memory-forget",),),
        mutates_state=True,
    ),
    CommandSpec(
        ("memory", "contradictions"),
        "Scan knowledge for conflicts with grounding rules.",
        "memory",
        aliases=(("contradiction-scan",),),
        mutates_state=True,
    ),
    CommandSpec(
        ("memory", "reindex"),
        "Rebuild the memory search index.",
        "memory",
        aliases=(("memory-reindex",),),
        mutates_state=True,
    ),
    CommandSpec(
        ("memory", "bootstrap"),
        "Promote observations into reusable knowledge.",
        "memory",
        aliases=(("memory-bootstrap",),),
        mutates_state=True,
    ),
    CommandSpec(("audit", "recent"), "Show recent audit events.", "evaluation"),
    CommandSpec(("audit", "show"), "Show one audit event.", "evaluation"),
    CommandSpec(("evaluate",), "Evaluate 1/3/5-day signal follow-through.", "evaluation"),
    CommandSpec(
        ("backup", "create"),
        "Create a local recovery archive.",
        "recovery",
        aliases=(("backup",),),
        mutates_state=True,
    ),
    CommandSpec(("backup", "inspect"), "Validate a recovery archive.", "recovery"),
    CommandSpec(
        ("restore",), "Preview or apply a recovery archive.", "recovery", mutates_state=True
    ),
    CommandSpec(
        ("export", "create"),
        "Create a private migration archive.",
        "recovery",
        aliases=(("export",),),
        mutates_state=True,
    ),
    CommandSpec(("export", "inspect"), "Validate a migration archive.", "recovery"),
    CommandSpec(
        ("import",), "Preview or apply a migration archive.", "recovery", mutates_state=True
    ),
    CommandSpec(
        ("compress",), "Compress stored context for one session.", "operations", mutates_state=True
    ),
    CommandSpec(
        ("service", "install"),
        "Install the persistent user service.",
        "operations",
        mutates_state=True,
    ),
    CommandSpec(
        ("service", "uninstall"),
        "Remove the persistent user service.",
        "operations",
        mutates_state=True,
    ),
    CommandSpec(
        ("service", "start"), "Start the persistent user service.", "operations", mutates_state=True
    ),
    CommandSpec(
        ("service", "stop"), "Stop the persistent user service.", "operations", mutates_state=True
    ),
    CommandSpec(
        ("service", "restart"),
        "Restart the persistent user service.",
        "operations",
        mutates_state=True,
    ),
    CommandSpec(
        ("service", "status"), "Show service and health status.", "operations", supports_json=True
    ),
    CommandSpec(
        ("service", "verify"),
        "Run a strict production-readiness check.",
        "operations",
        supports_json=True,
    ),
    CommandSpec(
        ("commands",), "List the canonical command catalog.", "reference", supports_json=True
    ),
)

_BY_PATH = {path: spec for spec in COMMAND_SPECS for path in (spec.path, *spec.aliases)}


def command_help(*path: str) -> str:
    return _BY_PATH[tuple(path)].summary


def command_catalog() -> list[dict[str, object]]:
    return [spec.to_dict() for spec in COMMAND_SPECS]


def render_command_catalog() -> str:
    categories: dict[str, list[CommandSpec]] = {}
    for spec in COMMAND_SPECS:
        categories.setdefault(spec.category, []).append(spec)
    lines: list[str] = []
    for category, specs in categories.items():
        lines.append(f"{category}:")
        for spec in specs:
            aliases = ""
            if spec.aliases:
                aliases = (
                    " (aliases: "
                    + ", ".join("trade-compass " + " ".join(alias) for alias in spec.aliases)
                    + ")"
                )
            lines.append(f"  {spec.command:<42} {spec.summary}{aliases}")
    return "\n".join(lines)
