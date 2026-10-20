"""SKILL.md → SkillSpec (compat facade over typed SkillManifest)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orchestrator.skills.common import render_prompt_block
from orchestrator.skills.manifest import (
    ParseProfile,
    SkillManifest,
    SkillParseError,
    parse_skill_manifest,
)


@dataclass(frozen=True)
class SkillSpec:
    """One skill pack loaded from ``skills/<id>/SKILL.md``.

    Prefer ``SkillManifest`` for new code. This DTO stays for existing callers.
    """

    id: str
    description: str
    instructions: str
    tools: tuple[str, ...] = ()
    allow_binaries: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    root: Path | None = None
    compatibility: str = ""
    license: str = ""
    when_to_use: str = ""
    argument_hint: str = ""
    disallowed_tools: tuple[str, ...] = ()
    raw_frontmatter: dict | None = None

    def prompt_block(self, *, body_limit: int = 8000) -> str:
        return render_prompt_block(
            self.id, self.description, self.instructions, body_limit=body_limit
        )


def skill_spec_from_manifest(manifest: SkillManifest) -> SkillSpec:
    return SkillSpec(
        id=manifest.command_name,
        description=manifest.description,
        instructions=manifest.body,
        tools=manifest.allowed_tools,
        allow_binaries=manifest.allow_binaries,
        tags=manifest.tags,
        root=manifest.root,
        compatibility=manifest.compatibility,
        license=manifest.license,
        when_to_use=manifest.when_to_use,
        argument_hint=manifest.argument_hint,
        disallowed_tools=manifest.disallowed_tools,
        raw_frontmatter=dict(manifest.raw_frontmatter),
    )


def parse_skill_md(
    path: Path,
    *,
    profile: ParseProfile | str = ParseProfile.PEON,
) -> SkillSpec:
    """Parse agentskills.io / Claude-style SKILL.md.

    Raises ``SkillParseError`` on malformed YAML or profile validation failure.
    Callers that must keep scanning should catch and record diagnostics.
    """
    manifest = parse_skill_manifest(Path(path), profile=profile)
    return skill_spec_from_manifest(manifest)


__all__ = [
    "ParseProfile",
    "SkillParseError",
    "SkillSpec",
    "parse_skill_md",
    "skill_spec_from_manifest",
]
