"""Skill style providers."""

from orchestrator.skills.providers.base import (
    CLAUDE_PROVIDER,
    DISCLOSURE_EAGER,
    DISCLOSURE_PROGRESSIVE,
    PEON_PROVIDER,
    SkillDocument,
    SkillId,
    SkillProvider,
    SkillSetup,
)
from orchestrator.skills.providers.claude_agent import ClaudeAgentSkillsProvider
from orchestrator.skills.providers.disk import DiskSkillsProvider
from orchestrator.skills.providers.peon_native import PeonNativeProvider

__all__ = [
    "CLAUDE_PROVIDER",
    "DISCLOSURE_EAGER",
    "DISCLOSURE_PROGRESSIVE",
    "PEON_PROVIDER",
    "ClaudeAgentSkillsProvider",
    "DiskSkillsProvider",
    "PeonNativeProvider",
    "SkillDocument",
    "SkillId",
    "SkillProvider",
    "SkillSetup",
]
