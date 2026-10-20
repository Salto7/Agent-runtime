"""Stable container mount names for skill roots."""

from __future__ import annotations

import re
from pathlib import Path


def extra_skill_mount(index: int, root: Path) -> str:
    """Return the container directory used for one configured external root.

    The index prevents two roots with the same basename from colliding while
    the basename keeps ``docker inspect`` and error messages understandable.
    """

    label = re.sub(r"[^a-z0-9._-]+", "-", root.name.lower()).strip("-") or "root"
    return f"/skills-extra/{int(index):02d}-{label}"
