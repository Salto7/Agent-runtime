"""First-party + classified packs under ``skills/<id>/``.

Same SKILL.md ABI as imported Claude Agent Skills. Provider is inferred per
pack (sidecar / metadata / frontmatter), not by a second directory tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from orchestrator.skills.providers.base import (
    DISCLOSURE_PROGRESSIVE,
    NATIVE_PROVIDER,
    SkillDocument,
    SkillProvider,
)
from orchestrator.skills.providers.disk import documents_from_root


class NativeProvider(SkillProvider):
    name = NATIVE_PROVIDER
    disclosure = DISCLOSURE_PROGRESSIVE

    def __init__(self, root: Path, *, container_base: str = "/skills") -> None:
        super().__init__(root, container_base=container_base)

    def discover(self) -> Sequence[SkillDocument]:
        return documents_from_root(
            self,
            source="native",
            skip_helpers=True,
            default_provider=NATIVE_PROVIDER,
        )
