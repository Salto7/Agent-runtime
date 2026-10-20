"""Skill style providers."""

from orchestrator.skills.providers.base import (
    CLAUDE_PROVIDER,
    DISCLOSURE_EAGER,
    DISCLOSURE_PROGRESSIVE,
    NATIVE_PROVIDER,
    SkillDocument,
    SkillId,
    SkillProvider,
    SkillSetup,
)
from orchestrator.skills.providers.claude_agent import ClaudeAgentSkillsProvider
from orchestrator.skills.providers.disk import DiskSkillsProvider
from orchestrator.skills.providers.native import NativeProvider

__all__ = [
    "CLAUDE_PROVIDER",
    "DISCLOSURE_EAGER",
    "DISCLOSURE_PROGRESSIVE",
    "NATIVE_PROVIDER",
    "ClaudeAgentSkillsProvider",
    "DiskSkillsProvider",
    "NativeProvider",
    "SkillDocument",
    "SkillId",
    "SkillProvider",
    "SkillSetup",
]
