from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ToolCategory = Literal["tool", "reader"]


class ToolPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class ToolDescriptor:
    name: str
    category: ToolCategory = "tool"
    description: str = ""


class ToolPolicy:
    def __init__(self, descriptors: list[ToolDescriptor]) -> None:
        self._descriptors = {descriptor.name: descriptor for descriptor in descriptors}

    def resolve(self, name: str) -> ToolDescriptor:
        descriptor = self._descriptors.get(name)
        if descriptor is None:
            raise ToolPolicyError(f"unknown tool: {name}")
        return descriptor

    def require_allowed(self, name: str, allowed: set[str] | None = None) -> ToolDescriptor:
        descriptor = self.resolve(name)
        if allowed is not None and name not in allowed:
            raise ToolPolicyError(f"tool not allowed: {name}")
        return descriptor

    def names(self, *, category: ToolCategory | None = None) -> set[str]:
        if category is None:
            return set(self._descriptors)
        return {name for name, descriptor in self._descriptors.items() if descriptor.category == category}


def default_tool_policy() -> ToolPolicy:
    from trade_compass_agent.runtime.tools.operations import BUILTIN_OPERATION_TOOL_SCHEMAS
    from trade_compass_agent.runtime.tools.readers import READER_TOOL_SCHEMAS
    from trade_compass_agent.runtime.tools.registry import BASE_TOOL_SCHEMAS, SCHEDULER_TOOL_SCHEMAS

    descriptors: list[ToolDescriptor] = []
    for schema in [*BASE_TOOL_SCHEMAS, *BUILTIN_OPERATION_TOOL_SCHEMAS, *SCHEDULER_TOOL_SCHEMAS]:
        function = schema.get("function") or {}
        name = str(function.get("name") or "")
        if name:
            descriptors.append(
                ToolDescriptor(
                    name=name,
                    category="tool",
                    description=str(function.get("description") or ""),
                )
            )
    for schema in READER_TOOL_SCHEMAS:
        function = schema.get("function") or {}
        name = str(function.get("name") or "")
        if name:
            descriptors.append(
                ToolDescriptor(
                    name=name,
                    category="reader",
                    description=str(function.get("description") or ""),
                )
            )
    return ToolPolicy(descriptors)
