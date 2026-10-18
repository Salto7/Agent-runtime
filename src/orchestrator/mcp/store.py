"""Storage contract for the MCP catalog — mirrors the brain's protocol
pattern (``orchestrator.brain.protocols``) but MCP servers are a catalog
concern, not a brain one, so it stays a separate, independent subpackage.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class McpRecord:
    mcp_id: str
    description: str = ""
    transport: str = "stdio"
    command: str = ""
    args: tuple[str, ...] = ()
    cwd: str = ""
    install: dict[str, Any] | None = None
    env_schema_full: tuple[dict[str, Any], ...] = ()
    reference_hosts: tuple[str, ...] = ()
    files: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def env_schema(self) -> tuple[tuple[str, bool], ...]:
        """Plain ``(name, required)`` pairs, for callers that don't need field types."""
        return tuple((f["name"], bool(f.get("required", True))) for f in self.env_schema_full)


@runtime_checkable
class McpStore(Protocol):
    def list_mcps(self) -> list[McpRecord]: ...
    def get_mcp(self, mcp_id: str) -> McpRecord | None: ...
    def upsert_mcp(self, record: McpRecord) -> McpRecord: ...
    def delete_mcp(self, mcp_id: str) -> None: ...


class InMemoryMcpStore:
    def __init__(self) -> None:
        self._records: dict[str, McpRecord] = {}

    def list_mcps(self) -> list[McpRecord]:
        return sorted(self._records.values(), key=lambda r: r.mcp_id)

    def get_mcp(self, mcp_id: str) -> McpRecord | None:
        return self._records.get(mcp_id)

    def upsert_mcp(self, record: McpRecord) -> McpRecord:
        record = replace(record, updated_at=utcnow())
        self._records[record.mcp_id] = record
        return record

    def delete_mcp(self, mcp_id: str) -> None:
        self._records.pop(mcp_id, None)
