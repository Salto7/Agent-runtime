"""Dynamic role↔skill affinity. Persisted affinity remains an optional cache.

Native packs: ``affinity.yaml`` next to SKILL.md.
Imported packs receive role bindings dynamically from their manifest content.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from orchestrator.skills.common import SKILL_NAME_RE
from orchestrator.skills.manifest import ParseProfile, SkillParseError, find_skill_manifest
from orchestrator.skills.model import parse_skill_md
from orchestrator.skills.providers.base import AFFINITY_NAME

logger = logging.getLogger(__name__)

AFFINITY_THRESHOLD = 0.5
_BODY_LIMIT = 2500

_CLASSIFY_SYSTEM = """You assign project roles to a catalog item (skill or MCP server).

Return ONLY JSON:
{
  "bindings": [
    {"role_id": "<catalog role id>", "score": 0.0-1.0, "lead": false}
  ]
}

Rules:
- role_id must be one of the provided catalog role ids (never invent ids)
- score is confidence that this role should own/run the item (0-1)
- include every role that reasonably fits; omit roles that do not
- at most one binding may have lead=true (default lead for that role)
- engagement-pm / analyzer roles only bind items clearly for that lane
- Learn/authoring-only packs may return bindings: []
- MCP servers usually bind to technical expert roles that would call their tools
- No prose outside JSON
"""

_WORD_RE = re.compile(r"[a-z0-9]+")
_ROLE_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "for",
        "of",
        "or",
        "the",
        "to",
        "with",
        "role",
        "expert",
        "specialist",
    }
)


@dataclass(frozen=True)
class AffinityBinding:
    role_id: str
    score: float = 1.0
    lead: bool = False


@dataclass(frozen=True)
class SkillAffinity:
    bindings: tuple[AffinityBinding, ...] = ()
    source: str = ""

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(b.role_id for b in self.bindings)

    @property
    def lead_roles(self) -> tuple[str, ...]:
        return tuple(b.role_id for b in self.bindings if b.lead)

    def roles_above(self, threshold: float = AFFINITY_THRESHOLD) -> tuple[str, ...]:
        return tuple(
            b.role_id for b in self.bindings if float(b.score) >= float(threshold)
        )


def affinity_path(skill_dir: Path) -> Path:
    return Path(skill_dir) / AFFINITY_NAME


def _clean_role(raw: object) -> str:
    role = str(raw or "").strip().lower()
    if role and SKILL_NAME_RE.match(role):
        return role
    return ""


def _parse_bindings(raw: Any) -> tuple[AffinityBinding, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[AffinityBinding] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            role = _clean_role(item)
            if not role or role in seen:
                continue
            seen.add(role)
            out.append(AffinityBinding(role_id=role, score=1.0, lead=False))
            continue
        if not isinstance(item, dict):
            continue
        role = _clean_role(item.get("role_id") or item.get("role") or item.get("id"))
        if not role or role in seen:
            continue
        try:
            score = float(item.get("score", 1.0))
        except (TypeError, ValueError):
            score = 1.0
        score = max(0.0, min(1.0, score))
        lead = bool(item.get("lead"))
        seen.add(role)
        out.append(AffinityBinding(role_id=role, score=score, lead=lead))
    return tuple(out)


def read_affinity(skill_dir: Path) -> SkillAffinity:
    path = affinity_path(skill_dir)
    if not path.is_file():
        return SkillAffinity()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return SkillAffinity()
    if not isinstance(data, dict):
        return SkillAffinity()
    return SkillAffinity(
        bindings=_parse_bindings(data.get("bindings") or data.get("roles")),
        source=str(data.get("source") or "").strip(),
    )


def _role_briefs() -> list[dict[str, str]]:
    from orchestrator.crew.roles.catalog import load_role_catalog

    briefs: list[dict[str, str]] = []
    for role in load_role_catalog():
        kind = role.kind
        if kind == "engagement-pm":
            goal = "Engagement lead; dispatch specialists; no probing."
        elif kind == "analyzer":
            goal = "Turn workspace evidence into findings and report.md."
        else:
            goal = f"Technical expert role `{role.id}` for in-lane engagement work."
        briefs.append({"id": role.id, "kind": kind, "goal": goal})
    return briefs


def score_roles(
    catalog: Iterable[Any], *, name: str, description: str, corpus: str
) -> tuple[AffinityBinding, ...]:
    """Lexical role match: a role token in the name > description > body."""
    name_words = set(_WORD_RE.findall(name.lower()))
    description_words = set(_WORD_RE.findall(description.lower()))
    words = set(_WORD_RE.findall(corpus.lower()))
    bindings: list[AffinityBinding] = []
    for role in catalog:
        tokens = {
            token
            for label in (role.id, *role.aliases)
            for token in _WORD_RE.findall(label.lower())
            if token not in _ROLE_STOPWORDS
        }
        if tokens & name_words:
            score = 0.95
        elif tokens & description_words:
            score = 0.75
        elif tokens & words:
            score = 0.55
        else:
            continue
        bindings.append(AffinityBinding(role_id=role.id, score=score, lead=score == 0.95))
    return tuple(bindings)


def keyword_role_bindings(
    *,
    name: str,
    description: str = "",
    body: str = "",
    tags: Iterable[str] = (),
) -> SkillAffinity:
    """Fast lexical role match from free text (no LLM)."""
    try:
        from orchestrator.crew.roles.catalog import load_role_catalog

        catalog = tuple(load_role_catalog())
    except Exception:
        return SkillAffinity()
    tag_text = " ".join(str(t) for t in tags)
    bindings = score_roles(
        catalog,
        name=str(name or ""),
        description=f"{description} {tag_text}",
        corpus=" ".join((name, description, tag_text, body[:4000])),
    )
    return SkillAffinity(bindings=bindings, source="dynamic-keyword")


def classify_text_affinity(
    *,
    owner_id: str,
    owner_kind: str,
    description: str = "",
    body: str = "",
    known_roles: Iterable[str] | None = None,
) -> SkillAffinity:
    """LLM-classify role bindings from catalog text. Empty on failure."""
    from orchestrator.utils.llm import chat_json, llm_configured

    if not llm_configured():
        return SkillAffinity()
    briefs = _role_briefs()
    if known_roles is not None:
        allow = {str(r).strip().lower() for r in known_roles if str(r).strip()}
        briefs = [b for b in briefs if b["id"] in allow]
    if not briefs:
        return SkillAffinity()
    text = (body or "").strip()
    if len(text) > _BODY_LIMIT:
        text = text[:_BODY_LIMIT].rstrip() + "\n…"
    human = (
        f"Item kind: {owner_kind}\n"
        f"Item id: {owner_id}\n"
        f"Description: {description}\n\n"
        f"Details:\n{text}\n\n"
        f"Catalog roles:\n{yaml.safe_dump(briefs, sort_keys=False)}"
    )
    try:
        from orchestrator.utils import llm_usage

        with llm_usage.purpose("skill_affinity"):
            data = chat_json(_CLASSIFY_SYSTEM, human)
    except Exception as exc:
        logger.warning("affinity classify failed for %s: %s", owner_id, exc)
        return SkillAffinity()
    if not isinstance(data, dict):
        return SkillAffinity()
    allowed = {b["id"] for b in briefs}
    raw_bindings = _parse_bindings(data.get("bindings"))
    kept = tuple(b for b in raw_bindings if b.role_id in allowed)
    return SkillAffinity(bindings=kept, source="llm")


def infer_catalog_bindings(
    *,
    owner_id: str,
    owner_kind: str,
    description: str = "",
    body: str = "",
    tags: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Auto role bindings for catalog save. Keyword first, LLM if empty."""
    keyed = keyword_role_bindings(
        name=owner_id,
        description=description,
        body=body,
        tags=tags,
    )
    affinity = keyed
    if not affinity.bindings:
        affinity = classify_text_affinity(
            owner_id=owner_id,
            owner_kind=owner_kind,
            description=description,
            body=body or description,
        )
    rows = [
        {"role_id": b.role_id, "score": b.score, "lead": bool(b.lead)}
        for b in affinity.bindings
        if float(b.score) >= AFFINITY_THRESHOLD
    ]
    # Catalog allows at most one lead across the whole owner.
    leads = [row for row in rows if row["lead"]]
    if len(leads) > 1:
        winner = max(leads, key=lambda row: (float(row["score"]), row["role_id"]))
        for row in rows:
            row["lead"] = row is winner
    return rows


def classify_skill_affinity(
    skill_dir: Path,
    *,
    known_roles: Iterable[str] | None = None,
) -> SkillAffinity:
    """LLM-classify role bindings for a pack. Returns empty affinity on failure."""
    root = Path(skill_dir)
    manifest = find_skill_manifest(root)
    if manifest is None:
        return SkillAffinity()
    try:
        spec = parse_skill_md(manifest, profile=ParseProfile.CLAUDE_CODE)
    except SkillParseError as exc:
        logger.debug("affinity parse failed for %s: %s", root, exc)
        return SkillAffinity()
    except OSError as exc:
        logger.debug("affinity read failed for %s: %s", root, exc)
        return SkillAffinity()
    return classify_text_affinity(
        owner_id=spec.id,
        owner_kind="skill",
        description=spec.description,
        body=spec.instructions or "",
        known_roles=known_roles,
    )

