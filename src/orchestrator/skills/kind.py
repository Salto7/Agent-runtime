"""Infer skill provider + parse profile from pack layout and frontmatter.

All packs live under ``skills/``. Type is not chosen by directory split.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from orchestrator.skills.common import split_frontmatter
from orchestrator.skills.manifest import (
    CLAUDE_EXTENSION_FIELDS,
    META_FIELDS,
    ParseProfile,
    parse_frontmatter_mapping,
)
from orchestrator.skills.providers.base import (
    AFFINITY_NAME,
    CLAUDE_PROVIDER,
    NATIVE_PROVIDER,
)

# Nested agent skill roots (path segments).
_AGENT_SKILL_MARKERS = frozenset(
    {
        ".claude",
        ".agents",
        ".agent",
        ".cursor",
    }
)

# metadata keys that are generic Agent Skills / Claude, not platform-specific.
_GENERIC_META = frozenset({"author", "version", "tags", "license", "provider"})

# Claude-only frontmatter keys that first-party packs do not use.
_CLAUDE_ONLY_FIELDS = frozenset(CLAUDE_EXTENSION_FIELDS) - {
    "allowed_tools",  # alias of allowed-tools; common to both
}


@dataclass(frozen=True)
class SkillKind:
    """Resolved skill style for one on-disk pack."""

    provider: str
    profile: ParseProfile
    reason: str


def _norm_provider(raw: object) -> str | None:
    text = str(raw or "").strip().lower().replace("\\", "/")
    if not text:
        return None
    if "/" in text:
        text = text.split("/", 1)[0]
    if text in {"native", "first-party"}:
        return NATIVE_PROVIDER
    if text in {"claude", "claude-code", "claude_code", "imported", "agentskills", "agent-skills"}:
        return CLAUDE_PROVIDER
    return None


def _load_frontmatter(root: Path, frontmatter: Mapping[str, Any] | None) -> dict[str, Any]:
    if frontmatter is not None:
        return dict(frontmatter)
    for name in ("SKILL.md", "skill.md"):
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        split = split_frontmatter(text)
        if not split:
            return {}
        try:
            return parse_frontmatter_mapping(split[0])
        except Exception:
            return {}
    return {}


def _under_agent_tree(root: Path) -> bool:
    parts = {p.lower() for p in root.parts}
    if not parts.intersection(_AGENT_SKILL_MARKERS):
        return False
    # Require …/<marker>/skills/… or …/<marker>/skill/…
    lowered = [p.lower() for p in root.parts]
    for i, part in enumerate(lowered):
        if part in _AGENT_SKILL_MARKERS and i + 1 < len(lowered):
            if lowered[i + 1] in {"skills", "skill"}:
                return True
    return False


def _has_native_sidecar(root: Path) -> bool:
    return (root / AFFINITY_NAME).is_file() or (root / "skills.lock.yaml").is_file()


def _has_native_metadata(fm: Mapping[str, Any]) -> bool:
    meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    if not isinstance(meta, dict):
        return False
    for key in meta:
        k = str(key).strip().lower()
        if k in META_FIELDS and k not in _GENERIC_META:
            return True
    return False


def _has_claude_only_fields(fm: Mapping[str, Any]) -> bool:
    normalized: set[str] = set()
    for key in fm:
        text = str(key).strip().lower()
        normalized.add(text)
        normalized.add(text.replace("_", "-"))
        normalized.add(text.replace("-", "_"))
    return bool(normalized.intersection(_CLAUDE_ONLY_FIELDS))


def infer_skill_kind(
    root: Path,
    *,
    frontmatter: Mapping[str, Any] | None = None,
    default_provider: str = NATIVE_PROVIDER,
) -> SkillKind:
    """Deduce provider + parse profile for a skill pack directory.

    Precedence (structured signals only — never prose scraping):
    1. ``provider`` / ``metadata.provider``
    2. Nested under ``.claude/skills`` (or agents/cursor equivalents) → claude
    3. ``affinity.yaml`` / ``skills.lock.yaml`` → native
    4. platform-specific ``metadata.*`` keys → native
    5. Claude-only frontmatter extensions → claude
    6. ``default_provider`` (``skills/`` → native; extra import roots → claude)
    """
    pack = Path(root)
    fm = _load_frontmatter(pack, frontmatter)
    default = _norm_provider(default_provider) or NATIVE_PROVIDER

    explicit = _norm_provider(fm.get("provider"))
    meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    if explicit is None and isinstance(meta, dict):
        explicit = _norm_provider(meta.get("provider"))
    if explicit == NATIVE_PROVIDER:
        return SkillKind(NATIVE_PROVIDER, ParseProfile.NATIVE, "frontmatter.provider")
    if explicit == CLAUDE_PROVIDER:
        return SkillKind(CLAUDE_PROVIDER, ParseProfile.CLAUDE_CODE, "frontmatter.provider")

    if _under_agent_tree(pack):
        return SkillKind(CLAUDE_PROVIDER, ParseProfile.CLAUDE_CODE, "agent_skills_path")

    if _has_native_sidecar(pack):
        return SkillKind(NATIVE_PROVIDER, ParseProfile.NATIVE, "native_sidecar")

    if _has_native_metadata(fm):
        return SkillKind(NATIVE_PROVIDER, ParseProfile.NATIVE, "native_metadata")

    if _has_claude_only_fields(fm):
        return SkillKind(CLAUDE_PROVIDER, ParseProfile.CLAUDE_CODE, "claude_extensions")

    if default == CLAUDE_PROVIDER:
        return SkillKind(CLAUDE_PROVIDER, ParseProfile.CLAUDE_CODE, "default_claude")
    return SkillKind(NATIVE_PROVIDER, ParseProfile.NATIVE, "default_native")


def catalog_id_for(name: str, kind: SkillKind) -> str:
    slug = str(name or "").strip().lower()
    if kind.provider == NATIVE_PROVIDER:
        return slug
    return f"{CLAUDE_PROVIDER}/{slug}"


__all__ = ["SkillKind", "catalog_id_for", "infer_skill_kind"]
