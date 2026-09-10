"""Stable, agent-facing capability definitions.

The diagnosis runtime should not need to know whether a data source is backed
by a Python SDK, an MCP server, a local executable, or an SSH probe.  This
module contains the small vocabulary that *is* visible to the Agent and the
mapping from a capability to the source kinds it can safely inspect.

Connectors may advertise additional capability IDs, but the core only exposes
capabilities that have a registered :class:`CapabilitySpec`.  Adding another
vendor implementation therefore does not require changing the runtime; adding
a genuinely new semantic operation requires one small spec and (normally) one
Agent-facing adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal

CapabilityEffect = Literal["read_only", "acquisition", "mutating"]


@dataclass(frozen=True)
class CapabilitySpec:
    """Describe one stable operation exposed to an investigation Agent."""

    id: str
    tool_name: str
    operation: str
    source_kinds: frozenset[str] = field(default_factory=frozenset)
    effect: CapabilityEffect = "read_only"
    requires_snapshot: bool = True

    def accepts_source_kind(self, source_kind: str) -> bool:
        return not self.source_kinds or source_kind in self.source_kinds


class CapabilityRegistry:
    """Deterministic registry for Agent-facing capability specifications."""

    def __init__(self, specs: Iterable[CapabilitySpec] | None = None) -> None:
        self._specs: dict[str, CapabilitySpec] = {}
        self._by_operation: dict[str, CapabilitySpec] = {}
        for spec in specs or ():
            self.register(spec)

    def register(self, spec: CapabilitySpec) -> CapabilitySpec:
        if not spec.id or not spec.tool_name or not spec.operation:
            raise ValueError("capability id, tool_name and operation are required")
        if spec.effect != "read_only":
            # The current Agent tool surface is deliberately read-only.  The
            # registry still models future acquisition/mutating operations so
            # they can be routed to an approval/job workflow instead of being
            # accidentally exposed as ordinary function tools.
            return self._register_non_agent_capability(spec)
        if spec.id in self._specs:
            raise ValueError(f"duplicate capability id: {spec.id}")
        existing = self._by_operation.get(spec.operation)
        if existing is not None:
            raise ValueError(f"duplicate capability operation: {spec.operation}")
        self._specs[spec.id] = spec
        self._by_operation[spec.operation] = spec
        return spec

    def _register_non_agent_capability(self, spec: CapabilitySpec) -> CapabilitySpec:
        if spec.id in self._specs:
            raise ValueError(f"duplicate capability id: {spec.id}")
        if spec.operation in self._by_operation:
            raise ValueError(f"duplicate capability operation: {spec.operation}")
        self._specs[spec.id] = spec
        self._by_operation[spec.operation] = spec
        return spec

    def get(self, capability_id: str) -> CapabilitySpec | None:
        return self._specs.get(capability_id)

    def for_operation(self, operation: str) -> CapabilitySpec | None:
        return self._by_operation.get(operation)

    def for_source(self, source_kind: str) -> list[CapabilitySpec]:
        return [
            spec
            for spec in self._specs.values()
            if spec.accepts_source_kind(source_kind)
        ]

    def all(self) -> list[CapabilitySpec]:
        return list(self._specs.values())

    @classmethod
    def default(cls) -> "CapabilityRegistry":
        """Return the built-in read-only operations.

        The ``*.v1`` IDs are persisted/configured identifiers.  The operation
        names remain the implementation-facing names used by legacy plugins.
        """

        return cls(
            [
                CapabilitySpec(
                    id="environment.describe.v1",
                    tool_name="describe_environment",
                    operation="describe_environment",
                    # This is a snapshot-level capability, not a capability
                    # of every individual source.  Keeping an explicit
                    # pseudo-kind prevents source discovery from presenting
                    # it as a knowledge/log/database operation.
                    source_kinds=frozenset({"environment"}),
                    requires_snapshot=True,
                ),
                CapabilitySpec(
                    id="database.describe.v1",
                    tool_name="describe_database",
                    operation="describe_database",
                    source_kinds=frozenset({"database"}),
                ),
                CapabilitySpec(
                    id="database.query.v1",
                    tool_name="query_database",
                    operation="query_database",
                    source_kinds=frozenset({"database"}),
                ),
                CapabilitySpec(
                    id="logs.search.v1",
                    tool_name="search_logs",
                    operation="search_logs",
                    source_kinds=frozenset({"logs"}),
                ),
                CapabilitySpec(
                    id="knowledge.search.v1",
                    tool_name="search_knowledge",
                    operation="search_knowledge",
                    source_kinds=frozenset({"knowledge"}),
                ),
                CapabilitySpec(
                    id="traffic.search.v1",
                    tool_name="search_traffic",
                    operation="search_traffic",
                    source_kinds=frozenset({"traffic"}),
                ),
            ]
        )


__all__ = ["CapabilityEffect", "CapabilityRegistry", "CapabilitySpec"]
