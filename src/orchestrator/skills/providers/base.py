"""Skill provider contract shared by every skill style."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from orchestrator.skills.common import SKILL_NAME_RE, render_prompt_block
from orchestrator.utils.paths import normalize_rel_path, safe_join

NATIVE_PROVIDER = "native"
CLAUDE_PROVIDER = "claude"

DISCLOSURE_EAGER = "eager"
DISCLOSURE_PROGRESSIVE = "progressive"

AFFINITY_NAME = "affinity.yaml"


@dataclass(frozen=True)
class SkillId:
    """``provider`` + skill ``name``. Bare strings address the native provider."""

    provider: str
    name: str

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.name}"

    @property
    def public(self) -> str:
        if self.provider == NATIVE_PROVIDER:
            return self.name
        return self.key

    @classmethod
    def parse(cls, raw: object) -> SkillId | None:
        text = str(raw or "").strip().replace("\\", "/")
        if not text or text.startswith("/") or "//" in text:
            return None
        if "/" in text:
            provider, _, name = text.partition("/")
        else:
            provider, name = NATIVE_PROVIDER, text
        provider = provider.strip().lower()
        name = name.strip().lower()
        if not SKILL_NAME_RE.match(provider) or not SKILL_NAME_RE.match(name):
            return None
        return cls(provider=provider, name=name)


@dataclass(frozen=True)
class SkillSetup:
    """Structured install hint. Never scraped from skill prose."""

    kind: str
    cwd: str = "."
    manifest: str = ""
    # One-off commands run after the dependency install (declared by the skill).
    post: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillDocument:
    """Normalized pack both providers return."""

    skill_id: SkillId
    description: str
    instructions: str
    root: Path | None
    tools: tuple[str, ...] = ()
    allow_binaries: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    disclosure: str = DISCLOSURE_EAGER
    role_ids: tuple[str, ...] = ()
    lead_roles: tuple[str, ...] = ()
    affinity_scores: tuple[tuple[str, float], ...] = ()
    compatibility: str = ""
    setup: SkillSetup | None = None
    unmapped_tools: tuple[str, ...] = ()
    # Absolute path inside the runtime. Host ``root`` is never interpolated
    # into a command, because external skill roots may live outside the platform's
    # repository and are mounted at a different location in Docker.
    container_root: str = ""
    container_setup_root: str = ""
    license: str = ""
    when_to_use: str = ""
    argument_hint: str = ""
    arguments: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    disable_model_invocation: bool = False
    user_invocable: bool = True
    model: str = ""
    context: str = ""
    agent: str = ""
    background: bool = True
    paths: tuple[str, ...] = ()
    shell: str = ""
    hooks: dict[str, object] | None = None
    raw_frontmatter: dict[str, object] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.skill_id.public

    @property
    def provider(self) -> str:
        return self.skill_id.provider

    @property
    def name(self) -> str:
        return self.skill_id.name

    def access_error(self, *, role_id: str = "") -> str:
        """Role-gated access. Matches ``SkillRegistry.usable``.

        Empty ``role_id`` skips the role check for native skills (catalog/Learn), but
        imported skills always require a bound role.
        """
        role = (role_id or "").strip().lower()
        if self.provider == NATIVE_PROVIDER:
            if not role:
                return ""
            if role not in self.role_ids:
                return f"Error: skill {self.id} is not bound to role {role}."
            return ""
        if not self.role_ids:
            return f"Error: skill {self.id} is not bound to any role."
        if not role:
            return f"Error: skill {self.id} requires a bound role."
        if role not in self.role_ids:
            return f"Error: skill {self.id} is not bound to role {role}."
        return ""

    def prompt_block(self, *, body_limit: int = 8000) -> str:
        return render_prompt_block(
            self.id, self.description, self.instructions, body_limit=body_limit
        )


class SkillProvider(ABC):
    """Discover and read one skill style. Does not execute or bind roles."""

    name: str
    disclosure: str

    def __init__(self, root: Path, *, container_base: str = "/skills") -> None:
        self.root = Path(root)
        self.container_base = (container_base or "/skills").rstrip("/") or "/skills"

    def container_path(self, root: Path) -> str:
        """Map a host path below this provider to its Docker mount path."""
        base = self.root.resolve()
        target = Path(root).resolve()
        try:
            relative = target.relative_to(base)
        except ValueError:
            relative = Path(target.name)
        if str(relative) == ".":
            return self.container_base
        return f"{self.container_base}/{relative.as_posix()}"

    @abstractmethod
    def discover(self) -> Sequence[SkillDocument]:
        """Packs under this provider root."""

    def mtime(self) -> float:
        from orchestrator.skills.registry_scan import skill_tree_mtime

        return skill_tree_mtime(self.root)

    def fingerprint(self) -> str:
        """Manifest/sidecar content stamp for registry cache invalidation."""
        from orchestrator.skills.registry_scan import skill_tree_fingerprint

        return skill_tree_fingerprint(self.root)

    def document(self, name: str) -> SkillDocument | None:
        wanted = (name or "").strip().lower()
        if not wanted:
            return None
        for doc in self.discover():
            if doc.name == wanted:
                return doc
        return None

    def list_resources(self, name: str) -> Sequence[str]:
        doc = self.document(name)
        if doc is None or doc.root is None:
            return ()
        return tuple(_pack_files(doc.root))

    def read_resource(self, name: str, path: str) -> bytes:
        doc = self.document(name)
        if doc is None or doc.root is None:
            raise FileNotFoundError(name)
        target = safe_join(doc.root, path)
        if target is None or not target.is_file():
            raise FileNotFoundError(path)
        return target.read_bytes()


def _pack_files(root: Path) -> list[str]:
    skip = {"node_modules", "__pycache__", ".git"}
    out: list[str] = []
    base = Path(root)
    if not base.is_dir():
        return out
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.name == AFFINITY_NAME:
            continue
        if skip.intersection(path.parts) or path.suffix in {".pyc", ".pyo"}:
            continue
        rel = path.relative_to(base).as_posix()
        if normalize_rel_path(rel) is None:
            continue
        out.append(rel)
    return out
