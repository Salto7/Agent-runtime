"""Platform catalog (roles, skills, MCP servers, bindings) — stdlib SQLite, no Django."""

from orchestrator.catalog.platform_catalog import (
    OWNER_MCP,
    OWNER_SKILL,
    Binding,
    McpRecord,
    PlatformCatalog,
    SkillRecord,
)
from orchestrator.catalog.store import configure_store

__all__ = [
    "Binding",
    "McpRecord",
    "OWNER_MCP",
    "OWNER_SKILL",
    "PlatformCatalog",
    "SkillRecord",
    "configure_store",
]
