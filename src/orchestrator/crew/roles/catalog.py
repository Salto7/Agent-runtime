"""Engagement role identity DTOs + defaults.

Rows live in the platform DB (``EngagementRole``). This module defines the
in-memory shape, seed defaults, and validation helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from orchestrator.skills.common import SKILL_NAME_RE

ROLE_KINDS = frozenset({"engagement-pm", "analyzer", "technical"})
BOOKEND_KINDS = frozenset({"engagement-pm", "analyzer"})


@dataclass(frozen=True)
class RoleDef:
    """One engagement role: identity and lane kind."""

    id: str
    kind: str  # engagement-pm | analyzer | technical
    aliases: tuple[str, ...] = ()
    protected: bool = False
    goal: str = ""
    backstory: str = ""

    @property
    def is_protected(self) -> bool:
        return bool(self.protected) or self.kind in BOOKEND_KINDS


# Inserted once when the role table is empty. Later edits live on the row.
DEFAULT_ROLES: tuple[RoleDef, ...] = (
    RoleDef(
        "project-manager",
        "engagement-pm",
        protected=True,
        goal=(
            "Review RoE and the objective sequence, dispatch specialists, "
            "recover from failures, replan when evidence changes, and hand "
            "off to the analyzer. Do not probe targets or record findings."
        ),
        backstory=(
            "You are the engagement lead. You govern technical experts, "
            "track objectives, and replan or ask the operator when blocked. "
            "Experts own discoveries; you do not probe targets."
        ),
    ),
    RoleDef(
        "reporter",
        "analyzer",
        protected=True,
        goal=(
            "From prior agents' workspace artifacts, batch-record structured "
            "findings then write findings/report.md. No probing."
        ),
        backstory=(
            "You are the reporting specialist. Turn workspace evidence into "
            "Finding rows and a standalone report. Never invent discoveries."
        ),
    ),
    RoleDef(
        "researcher",
        "technical",
        goal="Gather, verify and summarize information from the sources and tools available.",
        backstory="You lead research work: find evidence, cross-check it, and stay inside the rules of engagement.",
    ),
    RoleDef(
        "builder",
        "technical",
        goal="Write, run and verify scripts or small programs that produce the requested artifact.",
        backstory="You lead hands-on build work: implement, run, verify, and stay inside the rules of engagement.",
    ),
)


def normalize_role_id(raw: object) -> str:
    rid = str(raw or "").strip().lower()
    if not rid or not SKILL_NAME_RE.match(rid):
        return ""
    return rid


def normalize_aliases(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        parts = [str(p).strip() for p in raw]
    else:
        parts = []
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        alias = normalize_role_id(part)
        if not alias or alias in seen:
            continue
        seen.add(alias)
        out.append(alias)
    return tuple(out)


def normalize_kind(raw: object) -> str:
    kind = str(raw or "").strip().lower()
    return kind if kind in ROLE_KINDS else ""


def role_def_from_mapping(row: dict[str, Any]) -> RoleDef:
    rid = normalize_role_id(row.get("id") or row.get("role_id"))
    kind = normalize_kind(row.get("kind"))
    if not rid:
        raise ValueError("role id is required (kebab-case)")
    if not kind:
        raise ValueError(
            f"role {rid!r}: kind must be engagement-pm|analyzer|technical"
        )
    aliases = normalize_aliases(row.get("aliases"))
    aliases = tuple(a for a in aliases if a != rid)
    return RoleDef(
        id=rid,
        kind=kind,
        aliases=aliases,
        protected=bool(row.get("protected")),
        goal=str(row.get("goal") or "").strip(),
        backstory=str(row.get("backstory") or "").strip(),
    )


def seed_role_text(role_id: str, kind: str) -> tuple[str, str]:
    """Editable copy used when a role is first created."""
    rid = normalize_role_id(role_id)
    for role in DEFAULT_ROLES:
        if role.id == rid:
            return role.goal, role.backstory
    for role in DEFAULT_ROLES:
        if role.kind == kind and kind in BOOKEND_KINDS:
            return role.goal, role.backstory
    return (
        f"Lead authorized work for `{rid or 'this role'}` using the skills bound to it.",
        "You are a technical expert. Stay inside the rules of engagement.",
    )


def load_role_catalog() -> tuple[RoleDef, ...]:
    """Active roles from the platform catalog (defaults when DB unavailable)."""
    try:
        from orchestrator.catalog import PlatformCatalog

        return PlatformCatalog.shared().list_roles()
    except Exception:
        return DEFAULT_ROLES


def catalog_role_ids() -> list[str]:
    return [r.id for r in load_role_catalog()]


def role_alias_map(roles: Iterable[RoleDef] | None = None) -> dict[str, str]:
    rows = tuple(roles) if roles is not None else load_role_catalog()
    out: dict[str, str] = {}
    for row in rows:
        out[row.id] = row.id
        for alias in row.aliases:
            out[alias] = row.id
    return out


def resolve_role_id(role_id: str, roles: Iterable[RoleDef] | None = None) -> str:
    rid = (role_id or "").strip().lower()
    if not rid:
        return ""
    return role_alias_map(roles).get(rid, rid)


