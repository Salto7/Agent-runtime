"""Catalog tables (roles, skills, MCP servers, files, bindings, role↔skill memory)."""

from __future__ import annotations

from orchestrator.catalog.store import Model, flag, js, num, text


class _Hashed(Model):
    content_hash = text("")


class EngagementRole(Model):
    pk_fields = ("role_id",)
    _ordering = ("role_id",)
    role_id = text("")
    kind = text("")
    aliases = js([])
    goal = text("")
    backstory = text("")
    protected = flag(False)


class RoleSkillMemory(Model):
    pk_fields = ("role_id",)
    role_id = text("")
    skills = js([])
    counts = js({})
    last_source = text("")
    last_project_id = text("")


class CatalogSkill(_Hashed):
    pk_fields = ("skill_id",)
    _ordering = ("skill_id",)
    skill_id = text("")
    provider = text("native")
    description = text("")
    body = text("")
    manifest = js({})
    setup = js(None)

    @classmethod
    def _migrate(cls, conn) -> None:
        conn.execute(f"UPDATE {cls._table} SET provider = 'native' WHERE provider = 'native'")   # pre-rename rows


class CatalogMcp(_Hashed):
    pk_fields = ("mcp_id",)
    _ordering = ("mcp_id",)
    mcp_id = text("")
    description = text("")
    transport = text("stdio")
    command = text("")
    args = js([])
    cwd = text("")
    install = js(None)
    env_schema = js([])
    reference_hosts = js([])
    enabled = flag(True)


class CatalogFile(Model):
    pk_fields = ("owner_kind", "owner_id", "path")
    owner_kind = text("")
    owner_id = text("")
    path = text("")
    body = text("")


class CatalogBinding(Model):
    pk_fields = ("role_id", "owner_kind", "owner_id")
    role_id = text("")
    owner_kind = text("")
    owner_id = text("")
    score = num(1.0)
    lead = flag(False)


class ProjectMcpConfig(Model):
    """Per-project env values for an MCP server (secrets stay out of the catalog)."""

    pk_fields = ("project_id", "mcp_id")
    project_id = text("")
    mcp_id = text("")
    env = js({})
