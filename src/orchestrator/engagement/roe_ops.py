"""Rules-of-Engagement operations — the library port of Peon's ``roe_ops.py``.

Scope *values* authorize; discovered targets are only **candidates** until promoted. All functions take the control
plane (for its ``StateStore``) and a project id, and update the stored ``Roe`` under a per-project lock.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Iterable

from orchestrator.engagement.target_discovery import SubjectCanonicalizer
from orchestrator.engagement.target_shapes import (
    as_asset_name,
    coerce_targets,
    extract_targets,
    keep_stop_hosts,
    is_subject_type,
)
from orchestrator.host.state import Roe

if TYPE_CHECKING:
    from orchestrator.host.control import LocalControlPlane

logger = logging.getLogger(__name__)


def roe_block_reason(role_ids: Iterable[str] | None, scope: Iterable[Any] | None) -> str | None:
    """Fail-closed when technical probe roles have no in-scope *values*."""
    names = [str(n).strip() for n in (role_ids or []) if str(n).strip()]
    if coerce_targets(scope):
        return None
    needing = [n for n in names if n in SubjectCanonicalizer.active_probe_role_ids()]
    if not needing:
        return None
    return (
        "Rules of Engagement fail-closed: in_scope has no authorized targets for "
        f"{', '.join(needing)} (add targets or deduce from brief)"
    )


def _edit(plane: "LocalControlPlane", project_id: str, fn) -> Any:
    """Read-modify-write the project's Roe under a lock (creating an empty one if the project has none)."""
    with plane.store.lock(f"roe:{project_id}"):
        meta = plane.store.get_project(project_id)
        roe = meta.roe or Roe()
        result = fn(roe)
        plane.store.set_project(project_id, roe=roe)
        return result


def _escalate_scope_change(plane: "LocalControlPlane", project_id: str, added: list[dict[str, str]]) -> None:
    """Wake the resident governance PM so it can re-govern after a scope change."""
    if not added:
        return
    vals = ", ".join(t.get("value", "") for t in added if t.get("value"))[:300]
    try:
        plane.governance.notify(project_id, text=f"Scope changed: {len(added)} target(s) now in RoE in_scope: {vals}",
                                escalate=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("governor notify failed: %s", exc)


def add_candidates(plane: "LocalControlPlane", project_id: str, raw: Iterable) -> int:
    """Append discovered assets as candidates (not yet authorized); also ingest them into the graph."""
    incoming = coerce_targets(raw)
    if not incoming:
        return 0

    def apply(roe: Roe) -> int:
        existing = coerce_targets(roe.candidates)
        before = len(existing)
        existing.extend(incoming)
        roe.candidates = coerce_targets(existing)
        return len(roe.candidates) - before

    added = _edit(plane, project_id, apply)
    try:
        from orchestrator.engagement.asset_graph import AssetGraphEngine, Project as GraphProject

        with plane.store.lock(f"graph:{project_id}"):
            AssetGraphEngine(GraphProject(project_id, plane.store)).ingest(
                incoming, bucket="candidate", source="rules_of_engagement"
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("asset graph ingest failed: %s", exc)
    return added


def promote_candidates(plane: "LocalControlPlane", project_id: str, *, indices: list[int] | None = None,
                       all_candidates: bool = False) -> int:
    """Move selected (or all) candidates into ``in_scope``. Returns the number promoted."""
    chosen: list[dict[str, str]] = []

    def apply(roe: Roe) -> int:
        candidates = coerce_targets(roe.candidates)
        if not candidates:
            return 0
        if all_candidates:
            picked, remaining = candidates, []
        else:
            wanted = set(indices or [])
            picked = [c for i, c in enumerate(candidates) if i in wanted]
            remaining = [c for i, c in enumerate(candidates) if i not in wanted]
        if not picked:
            return 0
        scope = coerce_targets(roe.in_scope)
        scope.extend(picked)
        roe.in_scope = coerce_targets(scope)
        roe.candidates = remaining
        chosen.extend(picked)
        return len(picked)

    n = _edit(plane, project_id, apply)
    _escalate_scope_change(plane, project_id, chosen)
    return n


def authorize_operator_scope(plane: "LocalControlPlane", project_id: str, text: str) -> list[dict[str, str]]:
    """Merge operator-named assets into ``in_scope`` (operator chat / replan text = authorization)."""
    blob = text or ""
    incoming = extract_targets(blob)
    if not incoming and len(blob.strip()) >= 8:
        incoming = SubjectCanonicalizer.suggest_assets(blob)
    if not incoming:
        return []
    novel_box: list[dict[str, str]] = []

    def apply(roe: Roe) -> None:
        known = {t["value"].lower() for t in coerce_targets(roe.in_scope) + coerce_targets(roe.exclusions)
                 if t.get("value")}
        novel = [t for t in incoming if t.get("value", "").lower() not in known]
        if not novel:
            return
        scope = coerce_targets(roe.in_scope)
        scope.extend(novel)
        roe.in_scope = coerce_targets(scope)
        taken = {n["value"].lower() for n in novel}
        roe.candidates = [c for c in coerce_targets(roe.candidates) if c.get("value", "").lower() not in taken]
        novel_box.extend(novel)

    _edit(plane, project_id, apply)
    _escalate_scope_change(plane, project_id, novel_box)
    return novel_box


def provision_roe(plane: "LocalControlPlane", project_id: str, *, texts: Iterable[str] | None = None,
                  exclusions: list | None = None, authorization: str = "", title: str = "") -> tuple[Roe, list[str]]:
    """Ensure the project has a RoE. If ``in_scope`` is empty, deduce assets from the brief into it.

    Stored rows are re-validated against the asset-name shape contract; type labels are soft hints.
    """
    deduced: list[str] = []

    def apply(roe: Roe) -> Roe:
        title_label = (title or "").strip().lower()
        cleaned: list[dict[str, str]] = []
        for row in coerce_targets(roe.in_scope):
            typ, val = str(row.get("type") or ""), str(row.get("value") or "").strip()
            if not is_subject_type(typ):
                continue
            if typ == "asset":
                if as_asset_name(val, explicit=True) is None:
                    continue
                if title_label and val.lower() == title_label and as_asset_name(val) is None:
                    continue
            cleaned.append(row)
        roe.in_scope = cleaned
        if not cleaned:
            parts = [str(t or "") for t in (texts or []) if str(t or "").strip()]
            assets = extract_targets(*parts)
            if not assets:
                blob = "\n".join(p.strip() for p in parts if p.strip())
                if len(blob) >= 2:
                    assets = coerce_targets(SubjectCanonicalizer.suggest_assets(blob))
            if not assets and title:
                assets = extract_targets(title)
            if assets:
                roe.in_scope = assets
                deduced.extend(t["value"] for t in assets)
                if authorization and not roe.authorization_note:
                    roe.authorization_note = authorization
                elif not roe.authorization_note:
                    roe.authorization_note = "Auto-deduced from brief (operator did not supply Rules of Engagement)."
                if not coerce_targets(roe.seed):
                    bits = [s.strip() for s in (title, *(texts or [])) if (s or "").strip()]
                    if bits:
                        roe.seed = coerce_targets([{"type": "other", "value": " | ".join(bits)[:500]}])
        if exclusions is not None:
            with keep_stop_hosts():
                roe.exclusions = coerce_targets(exclusions)
        if authorization and not roe.authorization_note:
            roe.authorization_note = authorization
        return roe

    roe = _edit(plane, project_id, apply)
    return roe, deduced


def roe_add_path(plane: "LocalControlPlane", project_id: str, sandbox_path: str) -> None:
    """Authorize a project-local input path in RoE seed + in_scope."""
    path = (sandbox_path or "").strip()
    if not path:
        return

    def apply(roe: Roe) -> None:
        row = {"type": "file", "value": path}
        seed, scope = coerce_targets(roe.seed), coerce_targets(roe.in_scope)
        if not any(t.get("value") == path for t in seed):
            seed.append(row)
        if not any(t.get("value") == path for t in scope):
            scope.append(row)
        roe.seed, roe.in_scope = seed, scope

    _edit(plane, project_id, apply)


def roe_remove_path(plane: "LocalControlPlane", project_id: str, sandbox_path: str) -> None:
    """Drop a path value from RoE seed + in_scope (if present)."""
    if plane.store.get_project(project_id).roe is None:
        return
    path = (sandbox_path or "").strip()

    def apply(roe: Roe) -> None:
        roe.seed = [t for t in coerce_targets(roe.seed) if t.get("value") != path]
        roe.in_scope = [t for t in coerce_targets(roe.in_scope) if t.get("value") != path]

    _edit(plane, project_id, apply)
