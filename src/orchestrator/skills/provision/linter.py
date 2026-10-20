"""Provision-time SKILL.md checks (agentskills layout + product metadata).

Required: ``SKILL.md`` with YAML frontmatter ``name`` + ``description``, and
directory name matching ``name``. Optional resource roots (``scripts/``,
``assets/``, ``references/``) are never required — only broken in-body refs fail.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

import yaml

from orchestrator.skills.common import (
    LINT_LIFECYCLES,
    MAX_COMPATIBILITY_LEN,
    MAX_DESCRIPTION_LEN,
    MAX_NAME_LEN,
    LintIssue,
    extract_file_refs,
    find_manifest,
    normalize_category,
    resolve_resource,
    split_frontmatter,
    valid_skill_name,
)
from orchestrator.skills.manifest import (
    AGENTSKILLS_FIELDS,
    CLAUDE_EXTENSION_FIELDS,
    META_FIELDS,
    ParseProfile,
)
from orchestrator.utils.service import SharedServiceBase

# Product fields that belong under ``metadata:`` (native layout), not top-level.
METADATA_TOP_LEVEL_FIELDS = META_FIELDS


class SkillLinter(SharedServiceBase):
    @staticmethod
    def lint_dir(skill_dir: Path) -> dict[str, Any]:
        skill_dir = Path(skill_dir)
        manifest = find_manifest(skill_dir)
        if manifest is None:
            issue = LintIssue("error", "missing_manifest", "Missing required file: SKILL.md")
            return {"ok": False, "skill_dir": str(skill_dir), "issues": [issue.to_dict()]}
        try:
            text = manifest.read_text(encoding="utf-8")
        except OSError as exc:
            return {
                "ok": False,
                "skill_dir": str(skill_dir),
                "issues": [LintIssue("error", "read_error", str(exc)).to_dict()],
            }
        issues = SkillLinter.lint_text(text, skill_dir=skill_dir)
        return {
            "ok": not any(i.level == "error" for i in issues),
            "skill_dir": str(skill_dir),
            "skill_name": skill_dir.name,
            "issues": [i.to_dict() for i in issues],
        }

    @staticmethod
    def check(skill_dir: Path) -> dict[str, Any]:
        """Lint a skill directory; ``compatible`` is True when there are no errors."""
        result = SkillLinter.lint_dir(skill_dir)
        issues = list(result.get("issues") or [])
        errors = [i for i in issues if i.get("level") == "error"]
        warnings = [i for i in issues if i.get("level") == "warning"]
        return {
            "compatible": bool(result.get("ok")),
            "skill_name": result.get("skill_name") or Path(skill_dir).name,
            "skill_dir": result.get("skill_dir") or str(skill_dir),
            "issues": issues,
            "errors": errors,
            "warnings": warnings,
        }

    @staticmethod
    def lint_text(text: str, *, skill_dir: Path | None = None) -> list[LintIssue]:
        if not (text or "").strip():
            return [LintIssue("error", "empty", "SKILL.md is empty.")]
        split = split_frontmatter(text)
        if not split:
            return [
                LintIssue(
                    "error",
                    "missing_frontmatter",
                    "Missing YAML frontmatter (file must start with --- … ---).",
                )
            ]
        fm_raw, body = split
        try:
            data = yaml.safe_load(fm_raw)
        except yaml.YAMLError as exc:
            return [LintIssue("error", "yaml_syntax", f"Frontmatter YAML error: {exc}")]
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return [LintIssue("error", "yaml_mapping", "Frontmatter must be a YAML mapping.")]

        issues = SkillLinter._spec_issues(
            data,
            skill_dir,
            profile=ParseProfile.NATIVE,
        )
        issues.extend(SkillLinter._meta_issues(data.get("metadata") or {}))
        if not (body or "").strip():
            issues.append(LintIssue("warning", "empty_body", "Skill body is empty."))
        if skill_dir is not None:
            for ref in extract_file_refs(body):
                if resolve_resource(skill_dir, ref) is None:
                    issues.append(
                        LintIssue("error", "broken_ref", f"File reference `{ref}` not found.")
                    )
        return issues

    @staticmethod
    def _spec_issues(
        fm: dict[str, Any],
        skill_dir: Path | None,
        *,
        profile: ParseProfile = ParseProfile.NATIVE,
    ) -> list[LintIssue]:
        issues: list[LintIssue] = []
        allowed_top = set(AGENTSKILLS_FIELDS)
        if profile in {ParseProfile.CLAUDE_CODE, ParseProfile.NATIVE}:
            allowed_top |= set(CLAUDE_EXTENSION_FIELDS)
        for key in sorted(set(fm) - allowed_top):
            if key in METADATA_TOP_LEVEL_FIELDS:
                hint = (
                    "Use `jobable` under `metadata:` instead of top-level `taskable`."
                    if key == "taskable"
                    else f"Move `{key}` under `metadata:` (SKILL.md format)."
                )
                issues.append(LintIssue("warning", "legacy_top_level", hint))
            elif profile == ParseProfile.AGENTSKILLS:
                issues.append(
                    LintIssue(
                        "warning",
                        "unexpected_field",
                        f"Unexpected top-level field `{key}` (agentskills profile).",
                    )
                )
            else:
                issues.append(
                    LintIssue(
                        "warning",
                        "unexpected_field",
                        f"Unexpected top-level field `{key}`.",
                    )
                )
        name = fm.get("name")
        if name is None or not str(name).strip():
            if profile == ParseProfile.CLAUDE_CODE and skill_dir is not None:
                # Directory name is the default command name.
                pass
            else:
                issues.append(
                    LintIssue("error", "missing_name", "Missing required field: name")
                )
        else:
            issues.extend(
                SkillLinter._name_issues(str(name), skill_dir, profile=profile)
            )
        desc = fm.get("description")
        if desc is None or not str(desc).strip():
            if profile != ParseProfile.CLAUDE_CODE:
                issues.append(
                    LintIssue(
                        "error",
                        "missing_description",
                        "Missing required field: description",
                    )
                )
        elif not isinstance(desc, str):
            issues.append(
                LintIssue("warning", "description_type", "`description` should be a string.")
            )
        elif len(str(desc)) > MAX_DESCRIPTION_LEN:
            issues.append(
                LintIssue(
                    "error",
                    "description_length",
                    f"Description exceeds {MAX_DESCRIPTION_LEN} chars.",
                )
            )
        compat = fm.get("compatibility")
        if compat is not None and not isinstance(compat, str):
            issues.append(
                LintIssue("error", "compatibility_type", "`compatibility` must be a string")
            )
        elif isinstance(compat, str) and len(compat) > MAX_COMPATIBILITY_LEN:
            issues.append(
                LintIssue(
                    "error",
                    "compatibility_length",
                    f"Compatibility exceeds {MAX_COMPATIBILITY_LEN} chars.",
                )
            )
        if fm.get("metadata") is not None and not isinstance(fm["metadata"], dict):
            issues.append(LintIssue("error", "metadata_type", "`metadata` must be a mapping"))
        tools = fm.get("allowed-tools")
        if tools is not None and not isinstance(tools, (str, list, tuple)):
            issues.append(
                LintIssue(
                    "error",
                    "allowed_tools_type",
                    "`allowed-tools` must be a string or list of strings",
                )
            )
        return issues

    @staticmethod
    def _name_issues(
        name: str,
        skill_dir: Path | None,
        *,
        profile: ParseProfile = ParseProfile.NATIVE,
    ) -> list[LintIssue]:
        issues: list[LintIssue] = []
        name = unicodedata.normalize("NFKC", name.strip())
        if len(name) > MAX_NAME_LEN:
            issues.append(LintIssue("error", "name_length", f"Name exceeds {MAX_NAME_LEN} chars."))
        if name != name.lower() or name.startswith("-") or name.endswith("-") or "--" in name:
            issues.append(
                LintIssue("error", "name_format", f"Invalid skill name '{name}'.")
            )
        if not valid_skill_name(name):
            issues.append(LintIssue("error", "name_chars", f"Invalid skill name '{name}'."))
        if (
            profile == ParseProfile.NATIVE
            and skill_dir
            and unicodedata.normalize("NFKC", skill_dir.name) != name
        ):
            issues.append(
                LintIssue(
                    "error",
                    "name_dir_mismatch",
                    f"Directory '{skill_dir.name}' must match name '{name}'.",
                )
            )
        elif (
            profile != ParseProfile.NATIVE
            and skill_dir
            and unicodedata.normalize("NFKC", skill_dir.name) != name
        ):
            issues.append(
                LintIssue(
                    "warning",
                    "name_dir_mismatch",
                    f"Directory '{skill_dir.name}' differs from name '{name}' "
                    "(allowed outside native profile).",
                )
            )
        return issues

    @staticmethod
    def _meta_issues(meta: Any) -> list[LintIssue]:
        if not isinstance(meta, dict):
            return []
        issues: list[LintIssue] = []
        lc = str(meta.get("lifecycle") or "").strip().lower()
        if lc and lc not in LINT_LIFECYCLES:
            issues.append(
                LintIssue("error", "lifecycle_unknown", f"Unknown lifecycle {lc!r}.")
            )
        raw = meta.get("max_iterations")
        if raw is not None and str(raw).strip():
            try:
                if int(raw) < 1:
                    raise ValueError
            except (TypeError, ValueError):
                issues.append(
                    LintIssue(
                        "error",
                        "max_iterations_type",
                        "`metadata.max_iterations` must be a positive integer.",
                    )
                )
        cat_raw = meta.get("category")
        if cat_raw is not None and str(cat_raw).strip():
            cat = normalize_category(cat_raw)
            if cat not in {"builtin", "custom"}:
                issues.append(
                    LintIssue(
                        "warning",
                        "category_slug",
                        "`metadata.category` should be builtin or custom "
                        f"(got {cat_raw!r}).",
                    )
                )
        if "taskable" in meta:
            issues.append(
                LintIssue(
                    "warning",
                    "legacy_taskable",
                    "Use `metadata.jobable` instead of `metadata.taskable`.",
                )
            )
        return issues

