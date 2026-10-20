"""Install a marketplace pack with ``npx openskills install --universal``."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from orchestrator.skills.common import apply_candidate_namespace
from orchestrator.skills.install.place import place_pack
from orchestrator.skills.registry_scan import discover_skill_candidates


def _reject_source(source: str) -> str:
    text = str(source or "").strip()
    if not text or "\n" in text or "\r" in text or "\x00" in text:
        raise ValueError("skill source is empty or contains a newline")
    return text


def install_openskills(source: str, *, revision: str = "") -> list[str]:
    """Install into the imported root. Bytes on disk are the pack; SKILL.md is unchanged."""
    origin = _reject_source(source)
    with tempfile.TemporaryDirectory(prefix="orchestrator-openskills-") as tmp:
        try:
            env = os.environ.copy()
            env["CI"] = "1"
            proc = subprocess.run(
                ["npx", "-y", "openskills", "install", origin, "--universal", "-y"],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
                env=env,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "npx is required to install marketplace skills"
            ) from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(detail or "openskills install failed")
        candidates = discover_skill_candidates(
            Path(tmp) / ".agent" / "skills", source="openskills", skip_helpers=True
        )
        if not candidates:
            raise RuntimeError("openskills install produced no SKILL.md pack")
        ids: list[str] = []
        for candidate in candidates:
            apply_candidate_namespace(candidate)
            ids.append(place_pack(candidate.root, source=origin, revision=revision))
        return ids
