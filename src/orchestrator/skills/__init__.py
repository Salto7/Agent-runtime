"""Skills: SKILL.md packs."""

from __future__ import annotations

from typing import Any

from orchestrator.skills.manifest import (
    ParseProfile,
    SkillManifest,
    SkillParseError,
    tokenize_allowed_tools,
)
from orchestrator.skills.model import SkillSpec, parse_skill_md
from orchestrator.skills.registry import SkillRegistry

__all__ = [
    "ParseProfile",
    "SkillManifest",
    "SkillParseError",
    "SkillRegistry",
    "SkillSpec",
    "parse_skill_md",
    "role_skills_prompt_block",
    "tokenize_allowed_tools",
    "upsert_role_skills",
]


def __getattr__(name: str) -> Any:
    if name in {"role_skills_prompt_block", "upsert_role_skills"}:
        from orchestrator.skills import role_memory

        return getattr(role_memory, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
