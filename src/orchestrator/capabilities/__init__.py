"""Capability registry — named callables for authoring and command helpers."""

from orchestrator.capabilities.registry import (
    REGISTRY,
    CapabilityGroup,
    capability,
    default_allowed_tools,
    ensure_registered,
)

__all__ = [
    "CapabilityGroup",
    "REGISTRY",
    "capability",
    "default_allowed_tools",
    "ensure_registered",
]
