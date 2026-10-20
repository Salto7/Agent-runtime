"""Claude Agent Skills under an import / extra root (or ``skills/`` packs).

Packs share the ``skills/`` tree with Peon. Classification uses structured
signals; this provider defaults ambiguous packs to ``claude``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from orchestrator.skills.providers.base import (
    CLAUDE_PROVIDER,
    DISCLOSURE_PROGRESSIVE,
    SkillDocument,
    SkillProvider,
)
from orchestrator.skills.providers.disk import documents_from_root


class ClaudeAgentSkillsProvider(SkillProvider):
    name = CLAUDE_PROVIDER
    disclosure = DISCLOSURE_PROGRESSIVE

    def __init__(self, root: Path, *, container_base: str = "/skills") -> None:
        super().__init__(root, container_base=container_base)

    def discover(self) -> Sequence[SkillDocument]:
        return documents_from_root(
            self,
            source="imported",
            skip_helpers=False,
            default_provider=CLAUDE_PROVIDER,
        )
