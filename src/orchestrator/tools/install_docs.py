"""Parse explicit ``install:`` YAML fences from skill docs (InstallResolver #2)."""

from __future__ import annotations

import re
from typing import Any

import yaml

# Fence body must start with install: or verify: (same contract as skill-writer).
INSTALL_FENCE_RE = re.compile(
    r"```(?:ya?ml)?\s*\n((?:install:|verify:)[\s\S]*?)```", re.IGNORECASE
)


def install_steps_from_fences(text: str) -> list[dict[str, Any]]:
    """Return ``install`` step dicts from fenced YAML blocks (empty if none)."""
    steps: list[dict[str, Any]] = []
    for match in INSTALL_FENCE_RE.finditer(text or ""):
        try:
            raw = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            continue
        if isinstance(raw, dict) and isinstance(raw.get("install"), list):
            steps.extend(s for s in raw["install"] if isinstance(s, dict))
    return steps


def has_install_fence(text: str) -> bool:
    return bool(install_steps_from_fences(text))
