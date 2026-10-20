"""Typed SKILL.md manifest parser (Agent Skills + Claude Code profiles).

Runtime registry stays provider-owned. This module is the single frontmatter
contract: tokenize tools, validate by profile, and never swallow YAML errors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

import yaml

from orchestrator.skills.common import (
    MAX_COMPATIBILITY_LEN,
    MAX_DESCRIPTION_LEN,
    MAX_NAME_LEN,
    valid_skill_name,
)
from orchestrator.utils.strings import as_bool, as_str_list

# Open Agent Skills top-level fields.
AGENTSKILLS_FIELDS = frozenset(
    {"name", "description", "license", "compatibility", "allowed-tools", "metadata"}
)

# Claude Code extensions (accepted under profile=claude-code / peon).
CLAUDE_EXTENSION_FIELDS = frozenset(
    {
        "allowed_tools",
        "disallowed-tools",
        "disallowed_tools",
        "when_to_use",
        "when-to-use",
        "argument-hint",
        "argument_hint",
        "arguments",
        "disable-model-invocation",
        "disable_model_invocation",
        "user-invocable",
        "user_invocable",
        "model",
        "context",
        "agent",
        "background",
        "paths",
        "shell",
        "hooks",
    }
)

# Peon product fields that belong under metadata: (legacy top-level tolerated).
PEON_META_FIELDS = frozenset(
    {
        "category",
        "tags",
        "aliases",
        "lifecycle",
        "jobable",
        "taskable",
        "tools",
        "capabilities",
        "requires_clis",
        "toolkit",
        "allow_binaries",
        "suggested_tools",
        "version",
        "manually_created",
        "protected",
        "direct_answer",
        "max_iterations",
        "allowed_tools",
        "author",
    }
)


class ParseProfile(str, Enum):
    AGENTSKILLS = "agentskills"
    CLAUDE_CODE = "claude-code"
    PEON = "peon"


class SkillParseError(ValueError):
    """Structured frontmatter / SKILL.md parse failure."""

    def __init__(self, message: str, *, code: str = "parse_error") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SkillManifest:
    """Normalized skill frontmatter + body."""

    directory_name: str
    command_name: str
    description: str
    body: str
    license: str = ""
    compatibility: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    when_to_use: str = ""
    argument_hint: str = ""
    arguments: tuple[str, ...] = ()

    disable_model_invocation: bool = False
    user_invocable: bool = True
    model: str = ""
    context: str = ""
    agent: str = ""
    background: bool = True
    paths: tuple[str, ...] = ()
    shell: str = ""
    hooks: dict[str, object] | None = None

    # Peon runtime hints derived from frontmatter / metadata.
    allow_binaries: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    raw_frontmatter: dict[str, Any] = field(default_factory=dict)
    profile: str = ParseProfile.PEON.value
    root: Path | None = None

    @property
    def id(self) -> str:
        return self.command_name

    @property
    def tools(self) -> tuple[str, ...]:
        return self.allowed_tools

    @property
    def instructions(self) -> str:
        return self.body


def tokenize_allowed_tools(raw: Any) -> tuple[str, ...]:
    """Split ``allowed-tools`` with balanced parentheses (not bare ``str.split``).

    Accepts a YAML string or list. Commas and whitespace separate tokens outside
    parentheses; inside ``Bash(…)`` spaces are kept.
    """
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for item in raw:
            out.extend(tokenize_allowed_tools(item))
        return _uniq(out)
    if not isinstance(raw, str):
        text = str(raw).strip()
        return (text,) if text else ()

    text = raw.strip()
    if not text:
        return ()

    tokens: list[str] = []
    buf: list[str] = []
    depth = 0

    def flush() -> None:
        token = "".join(buf).strip().strip(",")
        buf.clear()
        if token:
            tokens.append(token)

    for ch in text:
        if ch == "(":
            depth += 1
            buf.append(ch)
            continue
        if ch == ")" and depth > 0:
            depth -= 1
            buf.append(ch)
            continue
        if depth == 0 and ch in {" ", "\t", "\n", "\r", ","}:
            flush()
            continue
        buf.append(ch)
    flush()
    return _uniq(tokens)


def _uniq(items: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


def _meta_str_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        k = str(key).strip()
        if not k or value is None:
            continue
        if isinstance(value, (dict, list, tuple)):
            continue
        out[k] = str(value).strip()
    return out


def _pick(fm: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in fm and fm[key] is not None:
            return fm[key]
    return None


def _as_str_tuple(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return tokenize_allowed_tools(raw) if ("(" in raw or " " in raw or "," in raw) else (
            (raw.strip(),) if raw.strip() else ()
        )
    return tuple(as_str_list(raw))


def _binaries_from(
    fm: Mapping[str, Any],
    meta: Mapping[str, Any],
    compatibility: str,
) -> tuple[str, ...]:
    raw = _pick(fm, "allow_binaries") or _pick(
        meta, "allow_binaries", "requires_clis", "suggested_tools", "toolkit"
    )
    binaries = tuple(as_str_list(raw))
    if binaries:
        return binaries
    # Structured compatibility "Requires x, y" — only when short and unambiguous.
    lower = compatibility.lower()
    if lower.startswith("requires "):
        rest = compatibility[9:].strip()
        if rest and len(rest) <= 120 and " when " not in lower:
            return tuple(as_str_list(rest))
    return ()


def _validate_name(name: str, *, profile: ParseProfile, directory_name: str) -> None:
    if not name:
        raise SkillParseError("Missing required field: name", code="missing_name")
    if len(name) > MAX_NAME_LEN:
        raise SkillParseError(
            f"Name exceeds {MAX_NAME_LEN} chars.",
            code="name_length",
        )
    if profile == ParseProfile.AGENTSKILLS and not valid_skill_name(name):
        raise SkillParseError(f"Invalid skill name {name!r}", code="name_chars")
    if profile == ParseProfile.PEON:
        if not valid_skill_name(name):
            raise SkillParseError(f"Invalid skill name {name!r}", code="name_chars")
        if directory_name and directory_name != name:
            raise SkillParseError(
                f"Directory {directory_name!r} must match name {name!r}",
                code="name_dir_mismatch",
            )


def _validate_description(description: str, *, profile: ParseProfile) -> None:
    if profile == ParseProfile.CLAUDE_CODE and not description:
        # Claude Code may default description empty; Agent Skills / Peon require it.
        return
    if not description:
        raise SkillParseError(
            "Missing required field: description",
            code="missing_description",
        )
    if len(description) > MAX_DESCRIPTION_LEN:
        raise SkillParseError(
            f"Description exceeds {MAX_DESCRIPTION_LEN} chars.",
            code="description_length",
        )


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    from orchestrator.skills.common import split_frontmatter

    return split_frontmatter(text)


def parse_frontmatter_mapping(fm_raw: str) -> dict[str, Any]:
    """Parse YAML frontmatter. Raises ``SkillParseError`` on invalid YAML."""
    try:
        data = yaml.safe_load(fm_raw)
    except yaml.YAMLError as exc:
        raise SkillParseError(f"Frontmatter YAML error: {exc}", code="yaml_syntax") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SkillParseError("Frontmatter must be a YAML mapping.", code="yaml_mapping")
    return data


def parse_skill_manifest(
    path: Path,
    *,
    profile: ParseProfile | str = ParseProfile.PEON,
    text: str | None = None,
) -> SkillManifest:
    """Parse ``SKILL.md`` into a typed manifest. Raises ``SkillParseError``."""
    if isinstance(profile, ParseProfile):
        prof = profile
    else:
        prof = ParseProfile(str(profile).strip().lower())
    manifest_path = Path(path)
    root = manifest_path.parent
    directory_name = root.name
    try:
        body_text = text if text is not None else manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillParseError(f"Could not read {manifest_path}: {exc}", code="read_error") from exc

    split = _split_frontmatter(body_text)
    if not split:
        if prof == ParseProfile.CLAUDE_CODE:
            # Claude may treat body-only files as skills named after the directory.
            name = directory_name.strip().lower()
            return SkillManifest(
                directory_name=directory_name,
                command_name=name,
                description="",
                body=body_text.strip(),
                profile=prof.value,
                root=root,
            )
        raise SkillParseError(
            "Missing YAML frontmatter (file must start with --- … ---).",
            code="missing_frontmatter",
        )

    fm_raw, body = split
    fm = parse_frontmatter_mapping(fm_raw)
    meta_raw = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    meta_strs = _meta_str_map(meta_raw)

    raw_name = fm.get("name")
    if raw_name is None or not str(raw_name).strip():
        if prof == ParseProfile.CLAUDE_CODE:
            command_name = directory_name.strip().lower()
        else:
            raise SkillParseError("Missing required field: name", code="missing_name")
    else:
        command_name = str(raw_name).strip().lower()

    description = str(fm.get("description") or "").strip()
    _validate_name(command_name, profile=prof, directory_name=directory_name.lower())
    _validate_description(description, profile=prof)

    compatibility = str(fm.get("compatibility") or "").strip()
    if compatibility and len(compatibility) > MAX_COMPATIBILITY_LEN:
        raise SkillParseError(
            f"Compatibility exceeds {MAX_COMPATIBILITY_LEN} chars.",
            code="compatibility_length",
        )

    allowed = tokenize_allowed_tools(
        _pick(fm, "allowed-tools", "allowed_tools")
        or _pick(meta_raw, "tools", "capabilities")
    )
    disallowed = tokenize_allowed_tools(
        _pick(fm, "disallowed-tools", "disallowed_tools")
    )

    hooks_raw = fm.get("hooks")
    hooks = hooks_raw if isinstance(hooks_raw, dict) else None

    tags = tuple(
        as_str_list(_pick(fm, "tags") or _pick(meta_raw, "tags"))
    )
    return SkillManifest(
        directory_name=directory_name,
        command_name=command_name,
        description=description,
        body=(body or "").strip(),
        license=str(fm.get("license") or "").strip(),
        compatibility=compatibility,
        metadata=meta_strs,
        allowed_tools=allowed,
        disallowed_tools=disallowed,
        when_to_use=str(
            _pick(fm, "when_to_use", "when-to-use") or ""
        ).strip(),
        argument_hint=str(
            _pick(fm, "argument-hint", "argument_hint") or ""
        ).strip(),
        arguments=_as_str_tuple(_pick(fm, "arguments")),
        disable_model_invocation=as_bool(
            _pick(fm, "disable-model-invocation", "disable_model_invocation"),
            default=False,
        ),
        user_invocable=as_bool(
            _pick(fm, "user-invocable", "user_invocable"),
            default=True,
        ),
        model=str(fm.get("model") or "").strip(),
        context=str(fm.get("context") or "").strip(),
        agent=str(fm.get("agent") or "").strip(),
        background=as_bool(fm.get("background"), default=True),
        paths=_as_str_tuple(_pick(fm, "paths")),
        shell=str(fm.get("shell") or "").strip(),
        hooks=hooks,
        allow_binaries=_binaries_from(fm, meta_raw, compatibility),
        tags=tags,
        raw_frontmatter=dict(fm),
        profile=prof.value,
        root=root,
    )


def find_skill_manifest(skill_dir: Path) -> Path | None:
    from orchestrator.skills.common import find_manifest

    return find_manifest(Path(skill_dir))


def load_skill_manifest(
    path: Path,
    *,
    profile: ParseProfile | str = ParseProfile.PEON,
) -> tuple[SkillManifest | None, tuple[str, ...]]:
    """Parse without raising: ``(manifest|None, error messages)``."""
    try:
        return parse_skill_manifest(path, profile=profile), ()
    except SkillParseError as exc:
        return None, (exc.message,)


__all__ = [
    "AGENTSKILLS_FIELDS",
    "CLAUDE_EXTENSION_FIELDS",
    "PEON_META_FIELDS",
    "ParseProfile",
    "SkillManifest",
    "SkillParseError",
    "find_skill_manifest",
    "load_skill_manifest",
    "parse_frontmatter_mapping",
    "parse_skill_manifest",
    "tokenize_allowed_tools",
]
