"""Lockfile for packs installed into ``skills/``."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from orchestrator.config import get_config


def lock_path() -> Path:
    """Prefer ``skills/skills.lock.json``; fall back to the legacy name."""
    root = Path(get_config().skills_dir)
    modern = root / "skills.lock.json"
    if modern.is_file():
        return modern
    legacy = root / "skills_imported.lock.json"
    if legacy.is_file():
        return legacy
    imported = Path(get_config().skills_imported_dir)
    if imported.resolve() != root.resolve():
        legacy_imported = imported / "skills_imported.lock.json"
        if legacy_imported.is_file():
            return legacy_imported
    return modern


def content_hash(skill_md: Path) -> str:
    digest = hashlib.sha256(skill_md.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def record(
    *,
    name: str,
    source: str,
    revision: str = "",
    skill_md: Path,
) -> None:
    path = lock_path()
    # Always write the modern path going forward.
    path = Path(get_config().skills_dir) / "skills.lock.json"
    data: dict = {"skills": {}}
    # Merge from whichever lock we can find.
    for candidate in (
        path,
        Path(get_config().skills_dir) / "skills_imported.lock.json",
        Path(get_config().skills_imported_dir) / "skills_imported.lock.json",
    ):
        if not candidate.is_file():
            continue
        try:
            loaded = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(loaded, dict) and isinstance(loaded.get("skills"), dict):
            data = loaded
            break
    data.setdefault("skills", {})
    data["skills"][name] = {
        "source": source,
        "revision": revision,
        "name": name,
        "content_hash": content_hash(skill_md),
        "provider": "claude",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
