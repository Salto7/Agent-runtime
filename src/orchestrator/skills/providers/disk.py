"""Load skill documents from a single on-disk root (peon + claude packs)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

from orchestrator.skills.kind import infer_skill_kind
from orchestrator.skills.manifest import SkillParseError, parse_skill_manifest
from orchestrator.skills.providers.base import (
    CLAUDE_PROVIDER,
    DISCLOSURE_PROGRESSIVE,
    PEON_PROVIDER,
    SkillDocument,
    SkillId,
    SkillProvider,
)
from orchestrator.skills.providers.setup import (
    find_setup,
    infer_skill_affinity,
    manifest_document_fields,
)
from orchestrator.skills.providers.tool_bridge import classify_claude_tools
from orchestrator.skills.registry_scan import discover_skill_candidates

logger = logging.getLogger(__name__)


def documents_from_root(
    provider: SkillProvider,
    *,
    source: str = "disk",
    skip_helpers: bool = True,
    default_provider: str = PEON_PROVIDER,
) -> list[SkillDocument]:
    """Discover packs under ``provider.root`` and classify each one."""
    root = provider.root
    if not root.is_dir():
        return []
    docs: list[SkillDocument] = []
    for candidate in discover_skill_candidates(
        root,
        source=source,
        skip_helpers=skip_helpers,
    ):
        kind = infer_skill_kind(
            candidate.root,
            default_provider=default_provider,
        )
        try:
            spec = parse_skill_manifest(candidate.manifest, profile=kind.profile)
        except SkillParseError as exc:
            logger.warning(
                "skill skipped %s (%s): %s",
                candidate.manifest,
                kind.reason,
                exc.message,
            )
            continue
        except OSError as exc:
            logger.warning("skill unreadable %s: %s", candidate.manifest, exc)
            continue
        parsed = SkillId.parse(spec.id)
        if parsed is None or not parsed.name:
            logger.warning("skill invalid id %s", spec.id)
            continue
        # Frontmatter name is bare; provider comes from kind inference.
        name = parsed.name
        if kind.provider == PEON_PROVIDER and candidate.root.name.lower() != name:
            logger.warning(
                "peon skill dir/name mismatch %s vs %s",
                candidate.root.name,
                name,
            )
            continue
        if kind.provider == CLAUDE_PROVIDER and candidate.root.name.lower() != name:
            logger.info(
                "claude skill dir %s registers as %s",
                candidate.root.name,
                name,
            )
        _, unmapped = classify_claude_tools(spec.tools)
        affinity = infer_skill_affinity(candidate.root, manifest=spec)
        roles = affinity.roles_above()
        scores = tuple(
            (binding.role_id, binding.score)
            for binding in affinity.bindings
            if binding.role_id in roles
        )
        setup, setup_root = find_setup(candidate.root, root)
        docs.append(
            SkillDocument(
                skill_id=SkillId(provider=kind.provider, name=name),
                description=spec.description,
                instructions=spec.instructions,
                root=spec.root or candidate.root,
                tools=tuple(spec.tools),
                allow_binaries=tuple(spec.allow_binaries),
                tags=tuple(spec.tags),
                disclosure=DISCLOSURE_PROGRESSIVE,
                role_ids=roles,
                lead_roles=tuple(r for r in affinity.lead_roles if r in roles),
                affinity_scores=scores,
                compatibility=spec.compatibility,
                **manifest_document_fields(spec),
                setup=setup,
                unmapped_tools=unmapped,
                container_root=provider.container_path(candidate.root),
                container_setup_root=(
                    provider.container_path(setup_root) if setup_root else ""
                ),
            )
        )
    return docs


class DiskSkillsProvider(SkillProvider):
    """One mount root that may contain both Peon and Claude Agent Skills packs."""

    name = "disk"
    disclosure = DISCLOSURE_PROGRESSIVE

    def __init__(
        self,
        root: Path,
        *,
        container_base: str = "/skills",
        default_provider: str = PEON_PROVIDER,
        skip_helpers: bool = True,
        source: str = "disk",
    ) -> None:
        super().__init__(root, container_base=container_base)
        self.default_provider = default_provider
        self.skip_helpers = skip_helpers
        self.source = source

    def discover(self) -> Sequence[SkillDocument]:
        return documents_from_root(
            self,
            source=self.source,
            skip_helpers=self.skip_helpers,
            default_provider=self.default_provider,
        )


__all__ = ["DiskSkillsProvider", "documents_from_root"]
