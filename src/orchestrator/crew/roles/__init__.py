"""Role catalog: platform DB + affinity skills → RoleSpec → CrewAI Agent factory."""

from orchestrator.crew.roles.catalog import (
    DEFAULT_ROLES,
    ROLE_KINDS,
    RoleDef,
    catalog_role_ids,
    resolve_role_id,
)
from orchestrator.crew.roles.model import RoleSpec, build_crew_agent
from orchestrator.crew.roles.registry import RoleRegistry

__all__ = [
    "DEFAULT_ROLES",
    "ROLE_KINDS",
    "RoleDef",
    "RoleRegistry",
    "RoleSpec",
    "build_crew_agent",
    "catalog_role_ids",
    "resolve_role_id",
]
