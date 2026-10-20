"""Pick roles via the engagement manager's skill + platform role store."""

from __future__ import annotations

import json

from orchestrator.crew.roles.catalog import catalog_role_ids, resolve_role_id
from orchestrator.crew.roles.registry import RoleRegistry
from orchestrator.skills.registry import SkillRegistry
from orchestrator.utils import llm_usage
from orchestrator.utils.llm import chat_text, llm_configured
from orchestrator.utils.service import SharedServiceBase
from orchestrator.utils.strings import extract_json

def _project_manager_prompt() -> str:
    """Guidance of the engagement manager role's lead skill (resolved, not named)."""
    manager = RoleRegistry.shared().manager()
    if manager is None:
        return ""
    registry = SkillRegistry.shared()
    skill_id = registry.lead_skill_for_role(manager.id) or next(iter(manager.skills or ()), "")
    skill = registry.get(skill_id) if skill_id else None
    return skill.prompt_block() if skill is not None else ""


class RoleRouter(SharedServiceBase):
    """Resolve role ids using Peon catalog; LLM guided by the manager role's skill."""

    def resolve(
        self,
        description: str,
        *,
        explicit: list[str] | None = None,
        project: bool = True,
    ) -> list[str]:
        catalog = {r.id for r in RoleRegistry.shared().list_roles()}
        allowed = set(catalog_role_ids()) & catalog
        if not allowed:
            allowed = catalog
        out: list[str] = []
        for raw in explicit or []:
            rid = resolve_role_id(str(raw or "").strip())
            if rid in catalog and rid not in out:
                out.append(rid)
        if out:
            return self._bookend(out, project=project) if project else out

        text = (description or "").strip()
        mgr_id, _ana_id = RoleRegistry.shared().engagement_bookends()
        if not text:
            return [mgr_id] if project and mgr_id else []

        system = _project_manager_prompt()
        learned = ""
        try:
            from orchestrator.skills.role_memory import role_skills_prompt_block

            learned = role_skills_prompt_block()
        except Exception:
            learned = ""
        if llm_configured() and allowed and system:
            try:
                with llm_usage.purpose("role_router"):
                    raw = chat_text(
                        system,
                        (
                            "Select engagement role ids for this brief. "
                            "Never invent ids.\n\n"
                            f"Brief:\n{text[:3000]}\n\nproject={project}\n"
                            f"Allowed role ids: {', '.join(sorted(allowed))}\n"
                            + (f"\n{learned}\n" if learned else "")
                            + 'Reply ONLY JSON: {"roles":["id",...],"reason":"short"}'
                        ),
                    )
                data = json.loads(extract_json(raw))
                picked = data.get("roles") if isinstance(data, dict) else []
                for item in picked or []:
                    rid = resolve_role_id(str(item or "").strip())
                    if rid in allowed and rid not in out:
                        out.append(rid)
            except Exception:
                out = []

        if not out and project and mgr_id:
            out = [mgr_id]
        return self._bookend(out, project=project) if project else out

    @staticmethod
    def _bookend(roles: list[str], *, project: bool) -> list[str]:
        if not project:
            return roles
        mgr_id, ana_id = RoleRegistry.shared().engagement_bookends()
        middle = [r for r in roles if r and r not in {mgr_id, ana_id}]
        out: list[str] = []
        if mgr_id:
            out.append(mgr_id)
        out.extend(middle)
        if ana_id:
            out.append(ana_id)
        return out

    @staticmethod
    def _first_sentence(text: str, cap: int = 280) -> str:
        flat = " ".join((text or "").split())
        end = flat.find(". ")
        cut = flat[: end + 1] if 0 < end < cap else flat[:cap]
        return cut.rstrip()

    def _capability_lines(self, role) -> list[str]:
        """What each skill and bound MCP server *does*, so plans pick by function.

        Ids alone (e.g. "provider/some-skill") invite picking by name.
        """
        from orchestrator.skills.registry import SkillRegistry

        out: list[str] = []
        skills = SkillRegistry.shared()
        for sid in role.skills or ():
            try:
                doc = skills.get(sid)
            except Exception:  # noqa: BLE001
                doc = None
            desc = self._first_sentence(getattr(doc, "description", "") if doc else "")
            if desc:
                out.append(f"    - skill `{sid}`: {desc}")
        try:
            from orchestrator.mcp.provision import bound_mcp_records

            for rec in bound_mcp_records(role.id):
                desc = self._first_sentence(rec.description or "")
                out.append(
                    f"    - mcp server `{rec.mcp_id}`: {desc or 'MCP server'} "
                    "(tools via list_mcp_tools / call_mcp_tool)"
                )
        except Exception:  # noqa: BLE001
            pass
        return out

    def roles_index_text(self, *, compact: bool = False) -> str:
        lines: list[str] = []
        if not compact:
            mgr = RoleRegistry.shared().manager()
            lines = [
                "Roles group skills (platform role store). Assign role_id; "
                "agent loads role.skills.",
                "Spawn is a Peon capability on technical / engagement-pm agents.",
            ]
            if mgr:
                lines.append(f"Engagement manager id: `{mgr.id}`.")
        for r in RoleRegistry.shared().list_roles():
            skills = ", ".join(r.skills) if r.skills else "-"
            binaries = ", ".join(r.allow_binaries) if r.allow_binaries else "-"
            if compact:
                flags = [r.kind] if r.kind else []
                flag_s = f" [{', '.join(flags)}]" if flags else ""
                goal = (r.goal or "").strip().splitlines()
                goal0 = goal[0][:160] if goal else r.label
                lines.append(
                    f"{r.id}{flag_s}: {goal0} | skills={skills} | binaries={binaries}"
                )
            else:
                reports = r.reports_to or "(none)"
                lines.append(
                    f"- `{r.id}` [{r.kind}] — {r.label}: {r.goal[:180]} "
                    f"skills=[{skills}] binaries=[{binaries}] reports_to={reports}"
                )
                lines.extend(self._capability_lines(r))
        return "\n".join(lines) if lines else "No roles loaded."
