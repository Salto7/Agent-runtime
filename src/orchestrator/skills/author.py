"""Author new skills as Claude Agent Skills.

First-party packs live under ``skills/``; anything created from here is a
skills-ref pack under the imported root (``claude/<name>``).
"""

from __future__ import annotations

import shlex
import tempfile
from pathlib import Path
from typing import Any

from orchestrator.skills.common import (
    LintIssue,
    dump_skill_md,
    extract_file_refs,
    parse_frontmatter_yaml,
    truncate_compat,
    valid_skill_name,
)
from orchestrator.skills.install.validate import validate_pack
from orchestrator.skills.manifest import tokenize_allowed_tools
from orchestrator.skills.providers.tool_bridge import tool_key
from orchestrator.utils.paths import normalize_rel_path, safe_join
from orchestrator.utils.strings import unique

SCRIPT_SH = "scripts/run.sh"
_SPEC_FIELDS = frozenset(
    {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}
)
_CLAUDE_TOOL_KEYS = frozenset({"bash", "read", "write", "edit"})
_CANONICAL_TOOL = {"bash": "Bash", "read": "Read", "write": "Write", "edit": "Edit"}
_NATIVE_META = frozenset(
    {
        "jobable",
        "taskable",
        "lifecycle",
        "category",
        "aliases",
        "requires_clis",
        "toolkit",
        "tools",
        "capabilities",
        "version",
        "protected",
        "direct_answer",
        "max_iterations",
        "manually_created",
    }
)


def portable_cli_script(binary: str) -> str:
    """Bash entry a Claude agent and the sandbox can both execute."""
    cli = (binary or "").strip()
    if not cli or any(ch in cli for ch in "\n\r\x00"):
        raise ValueError("binary name is required")
    return f'#!/usr/bin/env bash\nset -euo pipefail\nexec {shlex.quote(cli)} "$@"\n'


def _tool_tokens(raw: Any) -> list[str]:
    parts = list(tokenize_allowed_tools(raw))
    kept: list[str] = []
    for token in parts:
        key = tool_key(token)
        if key not in _CLAUDE_TOOL_KEYS:
            continue
        if "(" in token:
            kept.append(token)
        else:
            kept.append(_CANONICAL_TOOL[key])
    if not any(tool_key(token) == "bash" for token in kept):
        kept.insert(0, "Bash")
    if not any(tool_key(token) == "read" for token in kept):
        kept.append("Read")
    return unique(kept)


def shape_skill_md(
    skill_md: str,
    *,
    name: str,
    binary: str = "",
    entry: str = "",
) -> str:
    """Frontmatter limited to the Agent Skills spec, with Claude tool names."""
    parsed = parse_frontmatter_yaml(skill_md)
    if not parsed:
        data: dict[str, Any] = {}
        body = (skill_md or "").strip()
    else:
        data, body = parsed
    clean: dict[str, Any] = {}
    for key, value in data.items():
        if key in _SPEC_FIELDS and key != "metadata":
            clean[key] = value
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    portable_meta = {
        key: value for key, value in meta.items() if key not in _NATIVE_META and value is not None
    }
    if portable_meta:
        clean["metadata"] = portable_meta
    clean["name"] = name
    description = str(clean.get("description") or "").strip()
    if not description:
        description = f"Use {name} when the operator request matches this skill."
    clean["description"] = description
    clean["allowed-tools"] = " ".join(_tool_tokens(data.get("allowed-tools")))
    cli = (binary or "").strip()
    compat = clean.get("compatibility")
    if cli and not str(compat or "").strip():
        clean["compatibility"] = truncate_compat(f"Requires {cli}")
    elif isinstance(compat, str):
        clean["compatibility"] = truncate_compat(compat)
    script = (entry or "").strip()
    if script and "run_skill_script(" not in body:
        body = (
            f"{body.rstrip()}\n\n"
            "## Run\n"
            "```\n"
            f'run_skill_script("{name}", "{script}", args=\'["<args>"]\')\n'
            "```\n"
        )
    return dump_skill_md(clean, body.strip() + "\n")


def shape_files(
    files: dict[str, str],
    *,
    binary: str = "",
) -> dict[str, str]:
    """Drop native-only ``skill_entry`` wrappers. Add a bash entry when a CLI is known."""
    out = {
        str(key): str(value) for key, value in (files or {}).items() if normalize_rel_path(str(key))
    }
    native_only = [rel for rel, body in out.items() if "skill_entry" in body or "run_binary(" in body]
    for rel in native_only:
        out.pop(rel, None)
    cli = (binary or "").strip()
    if cli and not str(out.get(SCRIPT_SH) or "").strip():
        out[SCRIPT_SH] = portable_cli_script(cli)
    return out


def lint_claude_files(
    name: str,
    skill_md: str,
    files: dict[str, str] | None = None,
) -> dict[str, Any]:
    """skills-ref plus a portability check. This is the create-time gate."""
    issues: list[LintIssue] = []
    file_map = {str(k): str(v) for k, v in (files or {}).items()}
    if not valid_skill_name(name):
        issues.append(LintIssue("error", "name_chars", f"Invalid skill name '{name}'."))
    for rel, body in file_map.items():
        if normalize_rel_path(rel) is None:
            issues.append(LintIssue("error", "bad_path", f"Unsafe path {rel!r}."))
        if "skill_entry" in body or "run_binary(" in body:
            issues.append(
                LintIssue(
                    "error",
                    "native_runtime",
                    f"{rel} imports orchestrator helpers. Ship a normal script with a shebang.",
                )
            )
    known = set(file_map)
    for ref in extract_file_refs(skill_md):
        if ref not in known and not ref.startswith("http"):
            issues.append(LintIssue("error", "broken_ref", f"File reference `{ref}` not found."))
    spec_errors: list[str] = []
    if not any(issue.level == "error" for issue in issues):
        spec_errors = _skills_ref_errors(name, skill_md, file_map)
        for message in spec_errors:
            issues.append(LintIssue("error", "skills_ref", message))
    errors = [issue.to_dict() for issue in issues if issue.level == "error"]
    warnings = [issue.to_dict() for issue in issues if issue.level == "warning"]
    return {
        "compatible": not errors,
        "errors": errors,
        "warnings": warnings,
    }


def _skills_ref_errors(name: str, skill_md: str, files: dict[str, str]) -> list[str]:
    with tempfile.TemporaryDirectory(prefix=f"orchestrator-claude-{name}-") as tmp:
        root = Path(tmp) / name
        root.mkdir()
        (root / "SKILL.md").write_text(
            skill_md if skill_md.endswith("\n") else skill_md + "\n", encoding="utf-8"
        )
        for rel, body in files.items():
            path = safe_join(root, rel)
            if path is None:
                return [f"unsafe path {rel!r}"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                body if str(body).endswith("\n") else str(body) + "\n", encoding="utf-8"
            )
        try:
            return validate_pack(root)
        except RuntimeError as exc:
            return [str(exc)]




