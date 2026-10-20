"""Skill helpers: catalog rules + path/ref utilities.

Catalog aliases/categories live on each SKILL.md; this module only normalizes
slugs, names cores, and resolves scripts/assets/references paths.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

def render_prompt_block(
    skill_id: str, description: str, instructions: str, *, body_limit: int = 8000
) -> str:
    """Prompt section for one skill: heading, description, clipped body."""
    body = (instructions or "").strip()
    if len(body) > body_limit:
        body = body[:body_limit].rstrip() + "\n…"
    head = f"### Skill `{skill_id}`"
    if description:
        head = f"{head}\n{description.strip()}"
    return f"{head}\n\n{body}".strip() if body else head


# --- catalog rules -----------------------------------------------------------

# agentskills.io skill name / category slug shape.
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Lint also accepts planner "auto" before coerce to a concrete lifecycle.
LINT_LIFECYCLES = frozenset({"short", "long", "continuous", "auto"})

# Skill categories (Agent Skills metadata).
SKILL_CATEGORIES = frozenset({"builtin", "custom"})
CATEGORY_ALIASES = {
    "platform": "builtin",
    "core": "builtin",
}

# --- path / manifest ---------------------------------------------------------

RESOURCE_ROOTS = ("scripts", "assets", "references")
SKILL_MANIFEST = "SKILL.md"
MANIFEST_NAMES = (SKILL_MANIFEST, "skill.md")
MAX_NAME_LEN, MAX_DESCRIPTION_LEN, MAX_COMPATIBILITY_LEN = 64, 1024, 500

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)
REF_RE = re.compile(
    r"\]\(((?:scripts|assets|references)/[^)\s]+)\)"
    r"|`((?:scripts|assets|references)/[^`]+)`"
    r"|(?:^|\s)((?:scripts|assets|references)/[\w./-]+)",
    re.MULTILINE,
)


def valid_skill_name(name: str) -> bool:
    return bool((name or "").strip() and SKILL_NAME_RE.match(name.strip()))


def normalize_category(value: object) -> str:
    """Slug ``metadata.category`` → ``builtin`` | ``custom`` (or empty)."""
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    while "--" in text:
        text = text.replace("--", "-")
    if not text or not SKILL_NAME_RE.match(text):
        return ""
    text = CATEGORY_ALIASES.get(text, text)
    if text in SKILL_CATEGORIES:
        return text
    # Legacy domain categories (recon, web, …) collapse to custom.
    return "custom"


@dataclass(frozen=True)
class LintIssue:
    level: str
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def find_manifest(skill_dir: Path) -> Path | None:
    root = Path(skill_dir)
    for name in MANIFEST_NAMES:
        path = root / name
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    try:
        entries = root.iterdir()
    except OSError:
        return None
    matches: list[Path] = []
    for path in entries:
        if path.name.lower() != SKILL_MANIFEST.lower():
            continue
        try:
            if path.is_file():
                matches.append(path)
        except OSError:
            continue
    matches.sort()
    if matches:
        return matches[0]
    return None


def split_frontmatter(text: str) -> tuple[str, str] | None:
    m = FRONTMATTER_RE.match((text or "").strip())
    return (m.group(1), m.group(2) or "") if m else None


def parse_frontmatter_yaml(text: str) -> tuple[dict[str, Any], str] | None:
    """Parse SKILL.md into ``(frontmatter dict, body)`` or ``None`` if invalid.

    Soft helper for authoring drafts. Runtime discovery uses
    ``manifest.parse_skill_manifest`` which raises on YAML errors.
    """
    split = split_frontmatter(text)
    if not split:
        return None
    try:
        from orchestrator.skills.manifest import parse_frontmatter_mapping

        data = parse_frontmatter_mapping(split[0])
    except Exception:
        return None
    return (data, split[1])


def namespaced_slug(name: str, namespace: str = "") -> str:
    """kebab-safe ``name``, prefixed with ``namespace`` when both are set.

    Two differently-sourced packs (e.g. two imported plugin bundles) can ship
    a skill with the same leaf name; without a namespace prefix, whichever
    imports second silently overwrites the first in the catalog.
    """
    slug = str(name or "").strip().lower()
    ns = re.sub(r"[^a-z0-9]+", "-", str(namespace or "").strip().lower()).strip("-")
    if ns and ns != slug:
        return f"{ns}-{slug}"
    return slug


def apply_skill_namespace(skill_md: str, namespace: str) -> str:
    """Rewrite SKILL.md's frontmatter ``name:`` to a namespaced slug.

    Used when a pack was discovered under a nested ``<namespace>/skills/``
    directory (a plugin bundle), so its catalog id carries the namespace and
    can't collide with another bundle's same-named skill. A no-op when
    ``namespace`` is empty or the frontmatter can't be parsed — callers that
    need a hard guarantee should check the id landed as expected afterward.
    """
    import yaml

    ns = str(namespace or "").strip()
    if not ns:
        return skill_md
    split = split_frontmatter(skill_md)
    if not split:
        return skill_md
    fm_raw, body = split
    try:
        data = yaml.safe_load(fm_raw) or {}
    except yaml.YAMLError:
        return skill_md
    if not isinstance(data, dict):
        return skill_md
    current = str(data.get("name") or "").strip().lower()
    if not current:
        return skill_md
    data = dict(data)
    data["name"] = namespaced_slug(current, ns)
    return dump_skill_md(data, (body or "").strip() + "\n")


def apply_candidate_namespace(candidate: Any) -> None:
    """Rewrite one discovered pack's SKILL.md in place for its namespace.

    Shared by every installer that discovers packs via
    ``discover_skill_candidates`` (disk bootstrap, drag-drop/zip, openskills)
    so the namespace-disambiguation rewrite lives in exactly one place.
    ``candidate`` is a ``SkillCandidate`` (duck-typed here to avoid a circular
    import — ``registry_scan`` already imports from this module).
    """
    if not candidate.namespace:
        return
    try:
        text = candidate.manifest.read_text(encoding="utf-8")
    except OSError:
        return
    candidate.manifest.write_text(
        apply_skill_namespace(text, candidate.namespace), encoding="utf-8"
    )


def dump_skill_md(data: dict[str, Any], body: str) -> str:
    import yaml

    fm = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip()
    body_text = body if body.startswith("\n") else f"\n{body}" if body else "\n"
    return f"---\n{fm}\n---{body_text}"


def truncate_compat(text: str, *, limit: int = MAX_COMPATIBILITY_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def extract_file_refs(text: str) -> list[str]:
    refs: list[str] = []
    for m in REF_RE.finditer(text or ""):
        ref = next((g for g in m.groups() if g), None)
        if not ref:
            continue
        # Prose often ends with ``scripts/run.py.`` — drop trailing punctuation.
        cleaned = ref.strip().rstrip(".,;:)")
        if cleaned and cleaned not in refs:
            refs.append(cleaned)
    return refs


def list_resource_files(skill_dir: Path) -> list[str]:
    out: list[str] = []
    for root_name in RESOURCE_ROOTS:
        root = skill_dir / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.name.startswith(".") or "__pycache__" in path.parts:
                continue
            if path.suffix in {".pyc", ".pyo"}:
                continue
            out.append(path.relative_to(skill_dir).as_posix())
    return out


def resolve_resource(skill_dir: Path, relative_path: str) -> Path | None:
    if not relative_path or relative_path.startswith("/"):
        return None
    rel = relative_path.lstrip("./")
    candidates = [skill_dir / rel]
    for root_name in RESOURCE_ROOTS:
        prefix = f"{root_name}/"
        candidates.append(
            skill_dir / root_name / rel[len(prefix) :]
            if rel.startswith(prefix)
            else skill_dir / root_name / rel
        )
    if rel.startswith("assets/"):
        candidates.append(skill_dir / "scripts" / rel.removeprefix("assets/"))
    if rel.startswith("scripts/"):
        candidates.append(skill_dir / "assets" / rel.removeprefix("scripts/"))
    base = skill_dir.resolve()
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if str(resolved).startswith(str(base)) and resolved.is_file():
            return resolved
    return None
