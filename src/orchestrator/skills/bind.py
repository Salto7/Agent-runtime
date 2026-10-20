"""Resolve skill ids + effective tools/binaries for a role job."""

from __future__ import annotations

from typing import Iterable

from orchestrator.crew.roles.model import RoleSpec
from orchestrator.crew.tools.catalog import canonicalize_crew_tools
from orchestrator.skills.providers.base import (
    DISCLOSURE_EAGER,
    DISCLOSURE_PROGRESSIVE,
    SkillDocument,
)
from orchestrator.skills.providers.tool_bridge import PROGRESSIVE_TOOLS, classify_claude_tools
from orchestrator.skills.registry import SkillRegistry, available_skills_block

_PROGRESSIVE_HINT = (
    "Use `load_skill` for full instructions, `read_skill_resource` for files "
    "under the pack, and `run_skill_script` to run a script. "
    "`$SKILL_DIR` is the pack root inside the sandbox."
)


def resolve_skill_ids(
    role: RoleSpec,
    requested: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Skills a role may bind through dynamically inferred affinity."""
    req = [str(s).strip() for s in (requested or ()) if str(s).strip()]
    if not req:
        return SkillRegistry.shared().skills_for_role(role.id) or tuple(
            s for s in (role.skills or ()) if s and _bindable(str(s), role.id)
        )
    return tuple(sid for sid in req if _bindable(sid, role.id))


def _bindable(skill_id: str, role_id: str) -> bool:
    return SkillRegistry.shared().usable(skill_id, role_id=role_id) is not None


def load_skill_specs(
    skill_ids: Iterable[str],
    *,
    role_id: str = "",
) -> list[SkillDocument]:
    reg = SkillRegistry.shared()
    out: list[SkillDocument] = []
    for sid in skill_ids or ():
        key = str(sid).strip()
        if not key:
            continue
        skill = (
            reg.usable(key, role_id=role_id) if role_id else reg.get(key)
        )
        if skill is not None:
            out.append(skill)
    return out


def _bound_tool_names(skill: SkillDocument) -> tuple[str, ...]:
    if skill.disclosure != DISCLOSURE_PROGRESSIVE:
        return tuple(skill.tools or ())
    crew, _ = classify_claude_tools(skill.tools)
    return tuple(name for name in crew if name not in PROGRESSIVE_TOOLS)


def _uniq_lower(names: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        key = str(name).strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return tuple(out)


def progressive_tool_names(
    skill_ids: Iterable[str],
    *,
    role_id: str = "",
) -> tuple[str, ...]:
    docs = load_skill_specs(skill_ids, role_id=role_id)
    if any(doc.disclosure == DISCLOSURE_PROGRESSIVE for doc in docs):
        return PROGRESSIVE_TOOLS
    return ()


def effective_tools(role: RoleSpec, skill_ids: Iterable[str]) -> tuple[str, ...]:
    """Union of role + selected skill tools, mapped onto the CrewAI catalog."""
    names = (*(role.tools or ()),) + tuple(
        n
        for skill in load_skill_specs(skill_ids, role_id=role.id)
        for n in _bound_tool_names(skill)
    )
    seen: set[str] = set()
    out: list[str] = []
    for name in canonicalize_crew_tools(names):
        if name not in seen:
            seen.add(name)
            out.append(name)
    return tuple(out)


def effective_binaries(role: RoleSpec, skill_ids: Iterable[str]) -> tuple[str, ...]:
    """Allowlist for the bound skill set (empty = no gate).

    When skills are selected, only those packs contribute — the role-wide
    union is not forced in, so a focused job is not gated by sibling skills.
    """
    docs = load_skill_specs(skill_ids, role_id=role.id)
    if docs:
        return _uniq_lower(
            name for skill in docs for name in (skill.allow_binaries or ())
        )
    return _uniq_lower(role.allow_binaries or ())


def skills_prompt_block(
    skill_ids: Iterable[str],
    *,
    role_id: str = "",
) -> str:
    docs = load_skill_specs(skill_ids, role_id=role_id)
    eager = [doc for doc in docs if doc.disclosure == DISCLOSURE_EAGER]
    progressive = [doc for doc in docs if doc.disclosure == DISCLOSURE_PROGRESSIVE]
    parts: list[str] = []
    if eager:
        parts.append(
            "## Selected skills\n\n" + "\n\n".join(doc.prompt_block() for doc in eager)
        )
    if progressive:
        parts.append(available_skills_block(progressive))
        parts.append(_PROGRESSIVE_HINT)
    return "\n\n".join(parts)
