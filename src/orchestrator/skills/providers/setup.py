"""Dependency metadata discovery shared by skill providers."""

from __future__ import annotations

import os
from pathlib import Path

from orchestrator.skills.affinity import (
    SkillAffinity,
    classify_skill_affinity,
    read_affinity,
    score_roles,
)
from orchestrator.skills.manifest import (
    ParseProfile,
    SkillManifest,
    SkillParseError,
    find_skill_manifest,
    parse_skill_manifest,
)
from orchestrator.skills.providers.base import SkillSetup


# Ordered from the most explicit/portable file to the broad package marker.
_SETUP_FILES: tuple[tuple[str, str], ...] = (
    ("pip", "requirements.txt"),
    ("pip", "pyproject.toml"),
    ("npm", "package.json"),
)


def find_setup(root: Path, boundary: Path) -> tuple[SkillSetup | None, Path | None]:
    """Find dependency metadata in a pack or its containing repository.

    Searching upward lets a skill live below ``skills/<name>`` while sharing
    the repository's dependency file, but never escapes the configured root.
    """

    pack = Path(root).resolve()
    stop = Path(boundary).resolve()
    current = pack
    while True:
        for kind, filename in _SETUP_FILES:
            if (current / filename).is_file():
                return (
                    SkillSetup(
                        kind=kind,
                        cwd=os.path.relpath(current, pack),
                        manifest=filename,
                    ),
                    current,
                )
        if current == stop or current.parent == current:
            break
        current = current.parent
    return None, None


def manifest_document_fields(manifest: SkillManifest) -> dict[str, object]:
    """Map shared Claude/Agent-Skills fields onto ``SkillDocument``."""
    return {
        "license": manifest.license,
        "when_to_use": manifest.when_to_use,
        "argument_hint": manifest.argument_hint,
        "arguments": manifest.arguments,
        "disallowed_tools": manifest.disallowed_tools,
        "disable_model_invocation": manifest.disable_model_invocation,
        "user_invocable": manifest.user_invocable,
        "model": manifest.model,
        "context": manifest.context,
        "agent": manifest.agent,
        "background": manifest.background,
        "paths": manifest.paths,
        "shell": manifest.shell,
        "hooks": manifest.hooks,
        "raw_frontmatter": dict(manifest.raw_frontmatter),
    }


def infer_skill_affinity(
    root: Path,
    *,
    manifest: SkillManifest | None = None,
) -> SkillAffinity:
    """Infer role bindings without requiring a manual role sidecar.

    Fast lexical matching handles obvious skills locally. Ambiguous skills can
    use the existing LLM classifier when configured; an unavailable classifier
    never makes a skill undiscoverable.
    """
    pack = Path(root)
    try:
        from orchestrator.crew.roles.catalog import load_role_catalog

        catalog = tuple(load_role_catalog())
    except Exception:
        # A broken or unavailable role catalog must not hide otherwise valid
        # skill packs from the registry.
        catalog = ()
    catalog_ids = {role.id for role in catalog}
    cached = read_affinity(pack)
    if cached.bindings:
        valid = tuple(
            binding
            for binding in cached.bindings
            if binding.role_id in catalog_ids
        )
        if valid:
            return SkillAffinity(
                bindings=valid,
                source=cached.source or "sidecar",
            )

    spec = manifest
    if spec is None:
        manifest_path = find_skill_manifest(pack)
        if manifest_path is None:
            return SkillAffinity()
        try:
            spec = parse_skill_manifest(
                manifest_path,
                profile=ParseProfile.CLAUDE_CODE,
            )
        except (OSError, SkillParseError):
            return SkillAffinity()

    bindings = score_roles(
        catalog,
        name=spec.command_name,
        description=f"{spec.description} {spec.when_to_use} {' '.join(spec.tags)}",
        corpus=" ".join(
            (
                spec.command_name,
                spec.description,
                spec.license,
                spec.compatibility,
                spec.when_to_use,
                " ".join(spec.tags),
                spec.body[:4000],
                " ".join(spec.allowed_tools),
                " ".join(spec.allow_binaries),
            )
        ),
    )
    if bindings:
        return SkillAffinity(bindings=bindings, source="dynamic-keyword")
    try:
        return classify_skill_affinity(pack, known_roles=catalog_ids)
    except Exception:
        # Optional LLM dependencies must not prevent skill discovery.
        return SkillAffinity()
