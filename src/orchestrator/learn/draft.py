"""Skill draft helpers: catalog resolution and Claude Agent Skill shaping."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from orchestrator.skills.author import (
    SCRIPT_SH,
    lint_claude_files,
    shape_files,
    shape_skill_md,
)
from orchestrator.skills.common import parse_frontmatter_yaml, valid_skill_name
from orchestrator.tools.catalog import ToolCatalog
from orchestrator.tools.install_docs import has_install_fence
from orchestrator.utils.strings import as_str_list, unique

INSTALL_GUIDE = "references/INSTALL.md"
_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9._+-]{1,31}")
# Same install taxonomy as tools-suggestor / CatalogProvisioner.
CATALOG_INSTALL_TYPES = ("apt", "github_release", "git_clone", "pip", "custom")

# Re-export for authoring callers.
__all__ = [
    "CATALOG_INSTALL_TYPES",
    "CatalogResolution",
    "INSTALL_GUIDE",
    "authoring_prompt",
    "catalog_summaries",
    "draft_binary",
    "ensure_skill_entry",
    "filter_catalog",
    "finalize_skill_payload",
    "install_guide_markdown",
    "install_recipe_from_suggestion",
    "lint_draft",
    "parse_skill_payload",
    "propose_missing_tool",
    "prompt_catalog_hits",
    "proposed_tool_match",
    "skill_prompt",
    "skill_result_dict",
    "tool_replan_human",
    "tool_suggest_human",
    "tool_suggestion_from_payload",
    "valid_skill_name",
]


@dataclass
class CatalogResolution:
    matched: list[dict[str, str]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def mode(self) -> str:
        return "catalog_wrapper" if self.matched else "capability"

    @property
    def primary_binary(self) -> str:
        return self.matched[0]["binary"] if self.matched else ""


def catalog_summaries(*, limit: int = 80) -> list[dict[str, str]]:
    return ToolCatalog.shared().summaries(limit=limit)


def resolve_catalog_keys(keys: list[str]) -> CatalogResolution:
    matched: list[dict[str, str]] = []
    missing: list[str] = []
    seen_keys: set[str] = set()
    seen_ids: set[str] = set()
    catalog = ToolCatalog.shared()
    for raw in keys:
        key = (raw or "").strip().lower()
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        tool = catalog.lookup(key)
        if tool is None:
            missing.append(key)
            continue
        if tool.id in seen_ids:
            continue
        seen_ids.add(tool.id)
        matched.append(
            {
                "id": tool.id,
                "binary": (tool.binary or tool.id).strip(),
                "description": (tool.description or "")[:240],
            }
        )
    return CatalogResolution(matched=matched, missing=missing)


def prompt_catalog_hits(prompt: str) -> list[dict[str, str]]:
    """Catalog tools named in free text (match-only; ignores unknown tokens)."""
    return resolve_catalog_keys([t.lower() for t in _TOKEN_RE.findall(prompt or "")]).matched


def proposed_tool_match(suggestion: dict[str, Any]) -> dict[str, str]:
    """Normalize a tools-suggestor result into a catalog hit shape."""
    parsed = suggestion.get("parsed") if isinstance(suggestion.get("parsed"), dict) else {}
    tid = str(suggestion.get("id") or parsed.get("id") or "").strip()
    binary = str(parsed.get("binary") or tid).strip() or tid
    desc = str(parsed.get("description") or suggestion.get("notes") or "").strip()
    return {"id": tid, "binary": binary, "description": desc[:240]}


def skill_prompt(skill_name: str) -> str:
    """Read ``skills/<name>/references/PROMPT.md``."""
    from pathlib import Path

    from orchestrator.config import get_config

    path = Path(get_config().skills_dir) / skill_name / "references" / "PROMPT.md"
    if not path.is_file():
        raise FileNotFoundError(f"missing prompt for skill {skill_name}: {path}")
    return path.read_text(encoding="utf-8")


def tool_suggestion_from_payload(payload: dict[str, Any], *, author: str = "") -> dict[str, Any]:
    """Normalize tools-suggestor / OpenCode JSON into a recipe dict."""
    if not isinstance(payload, dict):
        raise RuntimeError("tools-suggestor returned non-object JSON")
    yaml_text = str(payload.get("yaml") or "").strip()
    if not yaml_text:
        raise RuntimeError("tools-suggestor returned empty yaml")
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"suggested YAML is invalid: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("suggested YAML must be a mapping")
    tid = str(payload.get("id") or parsed.get("id") or "").strip()
    if not tid:
        raise RuntimeError("suggested tool missing id")
    out: dict[str, Any] = {
        "id": tid,
        "yaml": yaml_text,
        "install_script": str(payload.get("install_script") or "").strip(),
        "notes": str(payload.get("notes") or "").strip(),
        "parsed": parsed,
    }
    if author:
        out["author"] = author
    return out


def install_recipe_from_suggestion(suggestion: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": suggestion["id"],
        "yaml": suggestion["yaml"],
        "install_script": suggestion.get("install_script") or "",
        "notes": suggestion.get("notes") or "",
    }


def tool_suggest_human(prompt: str) -> str:
    from orchestrator.prompts import CATALOG_INSTALL_PREFER

    return (
        f"Operator request:\n{prompt}\n\n"
        f"Target image: the Peon sandbox (Kali). {CATALOG_INSTALL_PREFER}\n\n"
        f"Existing catalog tools (avoid duplicate ids):\n"
        f"{yaml.safe_dump(catalog_summaries(), sort_keys=False)}"
    )


def tool_replan_human(
    *,
    prompt: str,
    yaml_text: str,
    install_script: str = "",
    error: str = "",
    feedback: str = "",
) -> str:
    from orchestrator.prompts import CATALOG_INSTALL_PREFER

    text = (prompt or "").strip() or "Revise the catalog install recipe."
    fix = (feedback or "").strip()
    parts = [
        f"Operator request:\n{text}",
        f"Previous YAML (failed install test):\n```yaml\n{yaml_text}\n```",
        f"Previous install script:\n```bash\n{install_script or '(none)'}\n```",
        f"Install/test error:\n{error or '(unknown)'}",
    ]
    if fix:
        parts.append(f"Operator fix guidance:\n{fix}")
    parts.append(
        "Produce a revised catalog YAML that is more likely to install in the "
        "Peon sandbox (Kali). Keep the same tool intent as the operator "
        "request; fix install/verify based on the error"
        + (" and fix guidance." if fix else ".")
        + f" {CATALOG_INSTALL_PREFER}\n"
        f"Existing catalog tools:\n{yaml.safe_dump(catalog_summaries(), sort_keys=False)}"
    )
    return "\n\n".join(parts)


def filter_catalog(catalog: list[dict[str, str]], tools: list[str] | None) -> list[dict[str, str]]:
    if not tools:
        return catalog
    wanted = {t.strip().lower() for t in tools if str(t).strip()}
    return [t for t in catalog if t["id"] in wanted] or catalog


def propose_missing_tool(
    prompt: str, suggest_fn
) -> tuple[dict[str, Any] | None, list[dict[str, str]], list[dict[str, Any]]]:
    """If prompt names no catalog CLI, call ``suggest_fn`` and shape recipes."""
    if prompt_catalog_hits(prompt):
        return None, [], []
    suggestion = suggest_fn(prompt)
    return (
        suggestion,
        [proposed_tool_match(suggestion)],
        [install_recipe_from_suggestion(suggestion)],
    )


def finalize_skill_payload(
    payload: dict[str, Any],
    *,
    prompt: str,
    proposed: list[dict[str, str]],
    install_recipes: list[dict[str, Any]],
    suggested: list[str] | None = None,
) -> tuple[str, str, dict[str, str], list[str], str, dict[str, Any]]:
    """parse → ensure_skill_entry → lint_draft (shared by chat-json + OpenCode)."""
    name, skill_md, files, sug = parse_skill_payload(
        {
            **payload,
            "suggested_tools": payload.get("suggested_tools") or suggested or [],
        }
    )
    if proposed and not sug:
        sug = [proposed[0]["id"]]
    skill_md, files, mode = ensure_skill_entry(
        skill_md,
        files,
        name=name,
        suggested_tools=sug,
        prompt=prompt,
        proposed=proposed,
        install_recipes=install_recipes,
    )
    lint = lint_draft(skill_md, files, mode=mode)
    return name, skill_md, files, sug, mode, lint


def skill_result_dict(
    *,
    name: str,
    skill_md: str,
    files: dict[str, str],
    notes: str,
    suggested: list[str],
    mode: str,
    lint: dict[str, Any],
    tool_suggestion: dict[str, Any] | None = None,
    author: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": name,
        "skill_md": skill_md,
        "files": files,
        "notes": notes,
        "suggested_tools": suggested,
        "mode": mode,
        "lint": lint,
    }
    if author:
        result["author"] = author
    if tool_suggestion is not None:
        result["tool_suggestion"] = install_recipe_from_suggestion(tool_suggestion)
    return result


def install_guide_markdown(*, yaml_text: str = "", notes: str = "", binary: str = "") -> str:
    """Portable ``references/INSTALL.md`` for a CLI that is not already on PATH."""
    block: dict[str, Any] = {}
    text = (yaml_text or "").strip()
    if text:
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError:
            parsed = None
        if isinstance(parsed, dict):
            if isinstance(parsed.get("install"), list):
                block["install"] = parsed["install"]
            if isinstance(parsed.get("verify"), list):
                block["verify"] = parsed["verify"]
            if not binary:
                binary = str(parsed.get("binary") or parsed.get("id") or "").strip()
    if not block.get("install"):
        raise ValueError("install recipe YAML must include an install: list")
    fence = yaml.safe_dump(block, sort_keys=False).rstrip()
    note = (notes or "").strip()
    cli = (binary or "").strip() or "the CLI"
    lines = [
        f"# Install ({cli})",
        "",
        "Install this CLI when it is not already on PATH.",
        "",
    ]
    if note:
        lines.extend([note, ""])
    lines.extend(["```yaml", fence, "```", ""])
    return "\n".join(lines)


def _cli_tokens(raw: Any) -> list[str]:
    out: list[str] = []
    for item in as_str_list(raw):
        out.extend(p.lower() for p in re.split(r"[\s,;]+", item) if p.strip())
    return unique(out)


def _clis_from_frontmatter(data: dict[str, Any]) -> list[str]:
    """CLI ids from ``requires_clis`` / ``toolkit`` only — never free prose."""
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    clis = _cli_tokens(meta.get("requires_clis") or meta.get("toolkit"))
    if not clis:
        clis = _cli_tokens(data.get("requires_clis") or data.get("toolkit"))
    return clis


def _merge_matched(primary: CatalogResolution, extra: CatalogResolution) -> CatalogResolution:
    """Union matched tools; keep ``missing`` only from ``primary`` (declared CLIs)."""
    seen = {m["id"] for m in primary.matched}
    matched = list(primary.matched)
    for item in extra.matched:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        matched.append(item)
    return CatalogResolution(matched=matched, missing=list(primary.missing))


def resolve_intent(
    *,
    skill_md: str,
    suggested_tools: list[str],
    prompt: str,
    proposed: list[dict[str, str]] | None = None,
) -> CatalogResolution:
    """Resolve catalog tools for a draft.

    Declared CLIs (``requires_clis`` / ``suggested_tools``) are looked up on disk.
    Prompt tokens are match-only. ``proposed`` entries (from tools-suggestor, not
    yet on disk) count as matched so the skill can wrap them as catalog_wrapper.
    """
    declared: list[str] = []
    parsed = parse_frontmatter_yaml(skill_md)
    if parsed:
        declared.extend(_clis_from_frontmatter(parsed[0]))
    declared.extend(t.strip().lower() for t in suggested_tools if t.strip())
    resolution = resolve_catalog_keys(declared)

    prompt_tokens = [t.lower() for t in _TOKEN_RE.findall(prompt or "")]
    if prompt_tokens:
        prompt_hits = CatalogResolution(
            matched=resolve_catalog_keys(prompt_tokens).matched, missing=[]
        )
        resolution = _merge_matched(resolution, prompt_hits)

    proposed_hits: list[dict[str, str]] = []
    proposed_ids: set[str] = set()
    for item in proposed or []:
        tid = str(item.get("id") or "").strip()
        if not tid or tid in proposed_ids:
            continue
        proposed_ids.add(tid)
        proposed_hits.append(
            {
                "id": tid,
                "binary": str(item.get("binary") or tid).strip() or tid,
                "description": str(item.get("description") or "")[:240],
            }
        )
    if proposed_hits:
        resolution = _merge_matched(
            resolution, CatalogResolution(matched=proposed_hits, missing=[])
        )
        # Declared ids covered by a proposed recipe are not "missing".
        resolution = CatalogResolution(
            matched=resolution.matched,
            missing=[m for m in resolution.missing if m not in proposed_ids],
        )
    return resolution


def draft_binary(skill_md: str) -> str:
    """Catalog CLI declared on a draft, before shaping strips Peon metadata."""
    resolution = resolve_intent(skill_md=skill_md, suggested_tools=[], prompt="")
    if resolution.mode != "catalog_wrapper":
        return ""
    return resolution.primary_binary


def lint_draft(
    skill_md: str, files: dict[str, str], *, mode: str = "catalog_wrapper"
) -> dict[str, Any]:
    """Create-time gate: the pack must validate as a Claude Agent Skill."""
    del mode
    parsed = parse_frontmatter_yaml(skill_md)
    name = str((parsed[0] if parsed else {}).get("name") or "").strip().lower()
    return lint_claude_files(name, skill_md, files)


def _ensure_install_guide(
    files: dict[str, str],
    skill_md: str,
    *,
    recipes: list[dict[str, Any]],
) -> tuple[str, dict[str, str]]:
    """Inject ``references/INSTALL.md`` from tools-suggestor recipes when needed."""
    out = dict(files)
    if not recipes:
        return skill_md, out
    guide = str(out.get(INSTALL_GUIDE) or "")
    if not has_install_fence(guide):
        sections: list[str] = []
        for recipe in recipes:
            yaml_text = str(recipe.get("yaml") or "").strip()
            if not yaml_text:
                continue
            sections.append(
                install_guide_markdown(
                    yaml_text=yaml_text,
                    notes=str(recipe.get("notes") or ""),
                    binary=str(recipe.get("id") or ""),
                )
            )
        if sections:
            out[INSTALL_GUIDE] = "\n".join(sections).rstrip() + "\n"
    if INSTALL_GUIDE not in skill_md and "INSTALL.md" not in skill_md:
        skill_md = (
            f"{skill_md.rstrip()}\n\n## Install\n\n"
            "If the required CLI is not on PATH, follow "
            f"[{INSTALL_GUIDE}]({INSTALL_GUIDE}).\n"
        )
    return skill_md, out


def ensure_skill_entry(
    skill_md: str,
    files: dict[str, str],
    *,
    name: str,
    suggested_tools: list[str],
    prompt: str,
    proposed: list[dict[str, str]] | None = None,
    install_recipes: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, str], str]:
    """Catalog CLI → portable ``scripts/run.sh``. Instructions-only when no CLI.

    Off-catalog recipes still land in ``references/INSTALL.md`` as plain steps.
    """
    out = dict(files)
    recipes = list(install_recipes or [])
    resolution = resolve_intent(
        skill_md=skill_md,
        suggested_tools=suggested_tools,
        prompt=prompt,
        proposed=proposed,
    )
    mode = resolution.mode
    binary = resolution.primary_binary if mode == "catalog_wrapper" else ""
    if recipes:
        skill_md, out = _ensure_install_guide(out, skill_md, recipes=recipes)
    out = shape_files(out, binary=binary)
    entry = SCRIPT_SH if SCRIPT_SH in out else ""
    skill_md = shape_skill_md(skill_md, name=name, binary=binary, entry=entry)
    return skill_md, out, mode


def parse_skill_payload(
    payload: dict[str, Any],
) -> tuple[str, str, dict[str, str], list[str]]:
    name = str(payload.get("name") or "").strip().lower()
    skill_md = str(payload.get("skill_md") or "").strip()
    files_raw = payload.get("files") or {}
    files = {str(k): str(v) for k, v in files_raw.items()} if isinstance(files_raw, dict) else {}
    suggested = [str(t).strip() for t in (payload.get("suggested_tools") or []) if str(t).strip()]
    if not valid_skill_name(name):
        raise RuntimeError(f"invalid skill name {name!r}")
    if not skill_md:
        raise RuntimeError("skill-writer returned empty skill_md")
    return name, skill_md, files, suggested


def authoring_prompt(
    prompt: str,
    catalog: list[dict[str, str]],
    *,
    proposed: list[dict[str, str]] | None = None,
    tool_recipes: list[dict[str, Any]] | None = None,
) -> str:
    on_disk = prompt_catalog_hits(prompt)
    proposed = list(proposed or [])
    hits = list(on_disk)
    seen = {h["id"] for h in hits}
    for item in proposed:
        if item["id"] not in seen:
            hits.append(item)
            seen.add(item["id"])
    mode = "catalog_wrapper" if hits else "capability"
    install_types = ", ".join(CATALOG_INSTALL_TYPES)
    lines = [
        f"Operator request:\n{prompt}\n",
        f"Catalog tools matched on disk:\n{yaml.safe_dump(on_disk or ['(none)'], sort_keys=False)}",
        f"Authoring mode: {mode}",
        "Default allowed-tools: Bash Read",
        "Write a Claude Agent Skill. It must pass skills-ref.",
        "The script stays a shebang that execs the CLI. The Run section calls "
        'run_skill_script("<name>", "scripts/run.sh", args=\'["<args>"]\').',
        "Do not use skill_entry or run_binary. Do not document a bare bash invocation.",
    ]
    if tool_recipes:
        lines.append(
            "CLI is NOT in tools/catalog yet. Host drafted install recipe(s) via "
            f"tools-suggestor (types: {install_types}).\n"
            f'- files["{SCRIPT_SH}"] is a bash script: `exec <binary> "$@"`\n'
            "- The Run section calls run_skill_script, not bash.\n"
            f'- REQUIRED files["{INSTALL_GUIDE}"] with a ```yaml fence '
            "starting with `install:` (same steps as the recipe).\n"
            f"- Link {INSTALL_GUIDE} from SKILL.md. Do not put apt/git/pip steps in the body.\n"
            f"{yaml.safe_dump(tool_recipes, sort_keys=False)}"
        )
    elif mode == "catalog_wrapper":
        lines.append(
            f"CLI is in tools/catalog. Emit {SCRIPT_SH} that execs that binary. "
            f"Do not add {INSTALL_GUIDE}."
        )
    else:
        lines.append(
            "No CLI required. Write instructions and, if needed, a normal script "
            "with a shebang. Do not invent a wrapper for an unknown binary."
        )
    lines.append(f"Available catalog tools:\n{yaml.safe_dump(catalog, sort_keys=False)}")
    return "\n".join(lines)
