"""Copy a validated pack into ``skills/``. Does not rewrite SKILL.md body."""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from orchestrator.config import get_config
from orchestrator.skills.common import split_frontmatter
from orchestrator.skills.install.lockfile import record
from orchestrator.skills.install.validate import validate_pack
from orchestrator.skills.manifest import ParseProfile, SkillParseError, find_skill_manifest
from orchestrator.skills.model import parse_skill_md
from orchestrator.skills.providers.base import CLAUDE_PROVIDER, NATIVE_PROVIDER, SkillId
from orchestrator.skills.registry import SkillRegistry
from orchestrator.utils.install_skip import copytree_ignore


def _frontmatter_name(skill_dir: Path) -> str:
    manifest = find_skill_manifest(skill_dir)
    if manifest is None:
        raise ValueError(f"{skill_dir} has no SKILL.md")
    try:
        spec = parse_skill_md(manifest, profile=ParseProfile.AGENTSKILLS)
    except SkillParseError as exc:
        raise ValueError(exc.message) from exc
    parsed = SkillId.parse(spec.id)
    if parsed is None or parsed.provider != NATIVE_PROVIDER:
        raise ValueError(f"{skill_dir.name} frontmatter name is not a bare skill id")
    return parsed.name


def _stamp_claude_provider(manifest: Path) -> None:
    """Record ``metadata.provider: claude`` so kind inference stays stable."""
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"could not read {manifest}: {exc}") from exc
    split = split_frontmatter(text)
    if not split:
        # Body-only Claude packs: wrap with minimal frontmatter.
        name = manifest.parent.name.strip().lower()
        fm = {
            "name": name,
            "description": "",
            "metadata": {"provider": CLAUDE_PROVIDER},
        }
        body = text.strip()
        dumped = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
        manifest.write_text(f"---\n{dumped}\n---\n\n{body}\n", encoding="utf-8")
        return
    fm_raw, body = split
    try:
        data = yaml.safe_load(fm_raw) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"frontmatter YAML error in {manifest}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{manifest} frontmatter must be a mapping")
    meta = data.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
    if str(meta.get("provider") or "").strip():
        return
    if str(data.get("provider") or "").strip():
        return
    meta = dict(meta)
    meta["provider"] = CLAUDE_PROVIDER
    data["metadata"] = meta
    dumped = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip()
    manifest.write_text(f"---\n{dumped}\n---\n\n{(body or '').strip()}\n", encoding="utf-8")


def place_pack(skill_dir: Path, *, source: str, revision: str = "") -> str:
    """Validate, reject name collisions, then copy into ``skills/``."""
    import tempfile

    root = Path(skill_dir)
    manifest = find_skill_manifest(root)
    if manifest is None:
        raise ValueError(f"{root} has no SKILL.md")
    try:
        manifest_matches = [
            path
            for path in root.iterdir()
            if path.is_file() and path.name.lower() == "skill.md"
        ]
    except OSError as exc:
        raise ValueError(f"could not inspect {root}: {exc}") from exc
    if len(manifest_matches) > 1:
        names = ", ".join(sorted(path.name for path in manifest_matches))
        raise ValueError(f"{root} contains duplicate skill manifests: {names}")
    name = _frontmatter_name(root)
    # skills-ref requires the directory name to match frontmatter ``name``.
    # Stage under the canonical folder when installing from docs/ or similar.
    staged_tmp = None
    work = root
    if root.name.lower() != name:
        staged_tmp = tempfile.TemporaryDirectory(prefix="orchestrator-skill-stage-")
        staged = Path(staged_tmp.name) / name
        shutil.copytree(root, staged, ignore=copytree_ignore())
        work = staged
        manifest = find_skill_manifest(work)
        if manifest is None:
            staged_tmp.cleanup()
            raise ValueError(f"{root} has no SKILL.md after staging")
    try:
        errors = validate_pack(work)
        if errors:
            raise ValueError("; ".join(errors))
        native = SkillRegistry.shared().get(name)
        if native is not None and native.provider == NATIVE_PROVIDER:
            raise ValueError(f"skill name {name} is already a native skill")
        dest_root = Path(get_config().skills_dir)
        dest_root.mkdir(parents=True, exist_ok=True)
        dest = dest_root / name
        if dest.exists() and dest.resolve() != work.resolve():
            shutil.rmtree(dest)
        if dest.resolve() != work.resolve():   # installing a skill from its own folder is a no-op copy
            shutil.copytree(work, dest, ignore=copytree_ignore())
        copied_manifest = dest / manifest.name
        canonical_manifest = dest / "SKILL.md"
        if copied_manifest != canonical_manifest:
            if canonical_manifest.exists():
                raise ValueError(
                    f"{root} contains both {manifest.name} and SKILL.md"
                )
            copied_manifest.rename(canonical_manifest)
        _stamp_claude_provider(canonical_manifest)
        record(name=name, source=source, revision=revision, skill_md=canonical_manifest)
        SkillRegistry.shared().invalidate()
        return name
    finally:
        if staged_tmp is not None:
            staged_tmp.cleanup()
