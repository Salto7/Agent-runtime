"""Role catalog: platform DB identities + affinity-indexed skill packs."""

from __future__ import annotations

import logging

from orchestrator.crew.roles.catalog import (
    RoleDef,
    catalog_role_ids,
    load_role_catalog,
    resolve_role_id,
)
from orchestrator.crew.roles.model import RoleSpec
from orchestrator.crew.tools.catalog import canonicalize_crew_tools
from orchestrator.skills.providers.base import DISCLOSURE_PROGRESSIVE
from orchestrator.skills.providers.tool_bridge import (
    PROGRESSIVE_TOOLS,
    classify_claude_tools,
)
from orchestrator.skills.registry import SkillRegistry
from orchestrator.utils.service import SharedServiceBase

logger = logging.getLogger(__name__)


def _label(role_id: str) -> str:
    return role_id.replace("-", " ").strip().title() or role_id


def _skill_crew_tools(skill) -> tuple[str, ...]:
    """Map Claude frontmatter tools onto crew names; drop progressive loaders."""
    if skill.disclosure == DISCLOSURE_PROGRESSIVE:
        crew, _ = classify_claude_tools(skill.tools)
        return tuple(name for name in crew if name not in PROGRESSIVE_TOOLS)
    return canonicalize_crew_tools(skill.tools or ())


def _uniq(items: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


def _build_role(
    defn: RoleDef,
    skills: SkillRegistry,
    *,
    manager_id: str = "",
) -> RoleSpec:
    skill_ids = skills.skills_for_role(defn.id)
    specs = [
        s
        for sid in skill_ids
        if (s := skills.get(sid)) is not None
    ]
    tools: list[str] = []
    binaries: list[str] = []
    tags: list[str] = []
    for skill in specs:
        tools.extend(_skill_crew_tools(skill))
        binaries.extend(
            str(name).strip().lower() for name in (skill.allow_binaries or ())
        )
        tags.extend(str(tag).strip().lower() for tag in (skill.tags or ()))

    descriptions = [s.description.strip() for s in specs if s.description.strip()]
    label = _label(defn.id)
    skill_list = ", ".join(f"`{s}`" for s in skill_ids) or "(none)"
    goal = (defn.goal or "").strip()
    backstory = (defn.backstory or "").strip()
    if defn.kind == "engagement-pm":
        if not goal:
            goal = (
                "Review RoE and the objective sequence, dispatch specialists, "
                "and hand off to the analyzer. Do not probe targets."
            )
        if not backstory:
            backstory = f"You are the engagement lead (`{defn.id}`)."
        reports_to = ""
        allow_delegation = True
        advanced_reasoning = True
        max_iter = 12
        caps = ("orchestrate", "replan", "handover")
    elif defn.kind == "analyzer":
        if not goal:
            goal = "Turn workspace evidence into findings and a report. No probing."
        if not backstory:
            backstory = f"You are the reporting specialist (`{defn.id}`)."
        reports_to = manager_id
        allow_delegation = False
        advanced_reasoning = False
        max_iter = 12
        caps = ("report",)
    else:
        if not goal:
            goal = descriptions[0] if descriptions else (
                f"Lead authorized work for `{defn.id}` using the skills bound to you."
            )
        if not backstory:
            backstory = (
                f"You are `{defn.id}`, a technical expert. Run the authorized "
                "scope as given. Stay in RoE scope."
            )
        reports_to = manager_id
        allow_delegation = False
        advanced_reasoning = False
        max_iter = 25
        caps = _uniq(tags) or ("recon", "expert")
        if descriptions:
            backstory = (
                backstory
                + "\n\nSkill focus:\n"
                + "\n".join(f"- {d}" for d in descriptions[:6])
            )
        backstory = f"{backstory}\n\nBound skills: {skill_list}".strip()

    return RoleSpec(
        id=defn.id,
        label=label,
        crew_role=label,
        goal=goal,
        backstory=backstory,
        tools=_uniq(list(tools)),
        allow_binaries=_uniq(binaries),
        reports_to=reports_to,
        capabilities=caps,
        skills=skill_ids,
        kind=defn.kind,
        allow_delegation=allow_delegation,
        reasoning=False,
        advanced_reasoning=advanced_reasoning,
        max_iter=max_iter,
    )


class RoleRegistry(SharedServiceBase):
    """Roles from the platform DB, skill groups from affinity index."""

    def __init__(self) -> None:
        self._by_id: dict[str, RoleSpec] | None = None
        self._stamp: str = ""

    def invalidate(self) -> None:
        self._by_id = None
        self._stamp = ""
        try:
            from orchestrator.utils.commands import clear_command_caches

            clear_command_caches()
        except Exception as exc:
            logger.warning("clearing command caches failed: %s", exc, exc_info=True)

    def _disk_stamp(self) -> str:
        skills = SkillRegistry.shared()
        try:
            skill_stamp = skills._disk_stamp()
        except Exception:
            skill_stamp = str(skills._disk_mtime())
        try:
            from orchestrator.catalog import PlatformCatalog

            cat_m = PlatformCatalog.shared().revision()
        except Exception:
            cat_m = 0.0
        return f"{skill_stamp}|{float(cat_m or 0.0)}"

    def _scan(self) -> dict[str, RoleSpec]:
        skills = SkillRegistry.shared()
        defs = load_role_catalog()
        manager_id = next((d.id for d in defs if d.kind == "engagement-pm"), "")
        return {
            defn.id: _build_role(defn, skills, manager_id=manager_id)
            for defn in defs
        }

    def get_registry(self) -> dict[str, RoleSpec]:
        stamp = self._disk_stamp()
        if self._by_id is None or stamp != self._stamp:
            self._by_id = self._scan()
            self._stamp = stamp
        return self._by_id

    def get(self, role_id: str) -> RoleSpec | None:
        rid = resolve_role_id(role_id or "")
        if not rid:
            return None
        return self.get_registry().get(rid)

    def list_roles(self) -> list[RoleSpec]:
        return sorted(self.get_registry().values(), key=lambda r: r.id)

    def require(self, role_id: str) -> RoleSpec:
        role = self.get(role_id)
        if role is None:
            known = ", ".join(r.id for r in self.list_roles()) or "(none)"
            raise KeyError(f"unknown role {role_id!r}; known: {known}")
        return role

    def primary_skill_ids(self, role_id: str) -> list[str]:
        """Lead skill(s) for an objective, not the full role armory."""
        rid = (role_id or "").strip()
        role = self.get(rid)
        if role is None:
            return []
        lead = SkillRegistry.shared().lead_skill_for_role(role.id)
        if lead:
            return [lead]
        skills = [str(s).strip() for s in (role.skills or ()) if str(s).strip()]
        return skills[:1]

    def objective_skill_ids(
        self,
        role_id: str,
        requested: list[str] | tuple[str, ...] | None = None,
    ) -> list[str]:
        """Clamp planner-selected skills to what the role may bind."""
        from orchestrator.skills.bind import resolve_skill_ids

        role = self.get((role_id or "").strip())
        if role is None:
            return [str(s).strip() for s in (requested or ()) if str(s).strip()]
        primary = self.primary_skill_ids(role.id)
        if role.kind != "technical" and not role.is_technical:
            return list(resolve_skill_ids(role, None)) or primary
        req = [str(skill).strip() for skill in (requested or ()) if str(skill).strip()]
        if not req:
            return primary
        selected = list(resolve_skill_ids(role, req))
        return selected or primary

    def manager(self) -> RoleSpec | None:
        return next(
            (
                role
                for role in self.list_roles()
                if role.kind == "engagement-pm" or role.is_manager
            ),
            None,
        )

    def analyzer(self) -> RoleSpec | None:
        manager = self.manager()
        manager_id = manager.id if manager else ""
        roles = self.list_roles()
        for role in roles:
            if (role.kind == "analyzer" or role.is_analyzer) and (
                not role.reports_to or role.reports_to == manager_id
            ):
                return role
        return next((role for role in roles if role.is_analyzer), None)

    @staticmethod
    def _is_specialist(role: RoleSpec, *, skip: set[str], manager_id: str) -> bool:
        if role.id in skip or role.is_manager or role.is_analyzer:
            return False
        if not role.is_technical:
            return False
        return not manager_id or role.reports_to == manager_id

    def specialists(
        self,
        role_ids: list[str] | tuple[str, ...] | None = None,
    ) -> list[RoleSpec]:
        manager = self.manager()
        analyzer = self.analyzer()
        skip = {role.id for role in (manager, analyzer) if role is not None}
        manager_id = manager.id if manager else ""
        requested = [str(role).strip() for role in (role_ids or ()) if str(role).strip()]
        if not requested:
            return [
                role
                for role in self.list_roles()
                if self._is_specialist(role, skip=skip, manager_id=manager_id)
            ]
        out: list[RoleSpec] = []
        seen: set[str] = set()
        for role_id in requested:
            role = self.get(role_id)
            if (
                role is None
                or role.id in seen
                or not self._is_specialist(
                    role,
                    skip=skip,
                    manager_id=manager_id,
                )
            ):
                continue
            seen.add(role.id)
            out.append(role)
        return out

    def engagement_bookends(self) -> tuple[str, str]:
        manager = self.manager()
        analyzer = self.analyzer()
        return (
            manager.id if manager else "",
            analyzer.id if analyzer else "",
        )


__all__ = [
    "RoleDef",
    "RoleRegistry",
    "catalog_role_ids",
]
