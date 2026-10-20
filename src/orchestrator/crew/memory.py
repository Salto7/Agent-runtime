"""Peon semantic memory — Cognee-backed (replaces CrewAI LanceDB memory)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from orchestrator import settings
from orchestrator.config import get_config
from orchestrator.crew.cognee_backend import CogneeBackend
from orchestrator.utils.service import SharedServiceBase

logger = logging.getLogger(__name__)


def memory_enabled() -> bool:
    return settings.cognee_enabled()


def peon_memory_enabled() -> bool:
    """Platform dataset ``peon`` writes/recalls (cross-project lessons)."""
    return memory_enabled() and settings.get_bool("PEON_MEMORY_ENABLED", True)


def project_dataset(project_id: str) -> str:
    pid = str(project_id or "").strip()
    return f"project_{pid}" if pid else "peon"


def job_session_id(job: Any) -> str:
    oid = str(getattr(job, "objective_id", "") or "").strip()
    jid = str(getattr(job, "id", "") or "").strip()
    if oid:
        return f"objective_{oid}"
    if jid:
        return f"job_{jid}"
    return ""


def record_payload(record: Any) -> dict[str, Any]:
    """Labeled JSON document for Cognee (raw structured feed)."""
    payload = getattr(record, "payload", None)
    if not isinstance(payload, dict):
        payload = {}
    return {
        "kind": str(getattr(record, "category", "") or "action").strip() or "action",
        "summary": str(getattr(record, "summary", "") or "").strip(),
        "event": str(getattr(record, "event", "") or "").strip(),
        "role_id": str(getattr(record, "role_id", "") or "").strip(),
        "project_id": str(getattr(record, "project_id", "") or "").strip(),
        "objective_id": str(getattr(record, "objective_id", "") or "").strip(),
        "job_id": str(getattr(record, "job_id", "") or "").strip(),
        "created_at": str(getattr(record, "created_at", "") or "").strip(),
        "payload": payload,
        "indications": _indications(record),
    }


def _indications(record: Any) -> list[str]:
    cat = str(getattr(record, "category", "") or "").strip().lower()
    event = str(getattr(record, "event", "") or "").strip().lower()
    tags: list[str] = []
    if cat:
        tags.append(cat)
    if cat in {"error", "gap"} or "fail" in event:
        tags.append("failure")
    if cat in {"action", "method"} or "prompt" in event or event in {
        "run_cli",
        "steer",
        "operator",
    }:
        tags.append("prompt")
    if cat == "outcome":
        tags.append("outcome")
    if cat == "replan":
        tags.append("replan")
    if cat == "handoff":
        tags.append("handoff")
    return tags


def remember_transcript(job: Any, record: Any) -> None:
    """Feed a raw labeled transcript record into Cognee (session + audit)."""
    if not memory_enabled():
        return
    project_id = str(getattr(job, "project_id", "") or getattr(record, "project_id", "") or "")
    if not project_id:
        return
    doc = record_payload(record)
    backend = CogneeBackend.shared()
    backend.remember(
        doc,
        dataset_name=project_dataset(project_id),
        session_id=job_session_id(job),
        self_improvement=False,
        run_in_background=True,
    )


def remember_peon(
    content: str,
    *,
    category: str = "digest",
    event: str = "",
    project_id: str = "",
    refs: list[str] | None = None,
) -> bool:
    """Write a labeled document into the platform ``peon`` dataset."""
    if not peon_memory_enabled():
        return False
    text = (content or "").strip()
    if not text:
        return False
    doc = {
        "kind": str(category or "digest").strip() or "digest",
        "summary": text[:8000],
        "event": str(event or "").strip(),
        "project_id": str(project_id or "").strip(),
        "refs": list(refs or []),
        "indications": [str(category or "digest"), "peon"],
    }
    result = CogneeBackend.shared().remember(
        doc,
        dataset_name="peon",
        session_id="",
        self_improvement=False,
        run_in_background=True,
    )
    return bool(result.get("ok"))


def recall_for_new_project(
    *,
    title: str = "",
    summary: str = "",
    scope_hints: list[str] | None = None,
    top_k: int = 8,
) -> str:
    """Recall peon-wide lessons to bootstrap a newly created project."""
    if not peon_memory_enabled():
        return ""
    # Lessons are retrieved by the *shape of the task* (title/summary). Target
    # identifiers are deliberately not part of the query: they only pull in
    # target-specific text, and gists are target-agnostic by construction.
    del scope_hints
    query = " ".join(
        b for b in (str(title or "").strip(), str(summary or "").strip()) if b
    ) or "engagement lessons methods failures replans"
    parts: list[str] = []
    try:
        from orchestrator.memory import store as memory_store

        hits = [
            (e, s)
            for e, s in memory_store.search(None, query, limit=top_k, mark=False)
            if s > 0 and e.kind == "lesson"
        ]
        parts += [
            f"- {e.name} — {e.description}\n  {(e.body or '').strip()}" for e, _ in hits
        ]
    except Exception as exc:  # noqa: BLE001
        logger.debug("local lesson search failed: %s", exc)
    result = CogneeBackend.shared().recall(query, datasets=["peon"], top_k=top_k)
    if result.get("ok") and not result.get("build_failed"):
        text = str(result.get("text") or "").strip()
        if text:
            parts.append(text)
    else:
        logger.warning(
            "peon memory bootstrap recall skipped: %s",
            (result.get("error") or "unavailable")[:300],
        )
    return "\n".join(parts).strip()


def bootstrap_path(project_id: str) -> Path | None:
    if not project_id:
        return None
    try:
        from orchestrator.utils.paths import safe_workspace_key

        root = Path(get_config().workspaces_dir)
        return root / safe_workspace_key(str(project_id)) / "memory_bootstrap.md"
    except Exception:
        return None


def write_project_bootstrap(project_id: str, text: str) -> Path | None:
    path = bootstrap_path(project_id)
    body = (text or "").strip()
    if path is None or not body:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Memory bootstrap (Cognee peon recall)\n\n" + body + "\n",
            encoding="utf-8",
        )
        return path
    except OSError as exc:
        logger.debug("memory bootstrap write failed: %s", exc)
        return None


class _JobRow:
    """Attribute view over a host job dict (for ``job_session_id``)."""

    def __init__(self, row: dict[str, Any]) -> None:
        self.id = row.get("id", "")
        self.objective_id = row.get("objective_id", "")






class MemoryService(SharedServiceBase):
    """Cognee-backed project and Peon memory lifecycle."""

    enabled = staticmethod(memory_enabled)
    remember_transcript = staticmethod(remember_transcript)
    remember_peon = staticmethod(remember_peon)
    recall_for_new_project = staticmethod(recall_for_new_project)
    write_project_bootstrap = staticmethod(write_project_bootstrap)

    @classmethod
    def health(cls) -> dict[str, Any]:
        return CogneeBackend.shared().health()

    @classmethod
    def recall(
        cls,
        query: str,
        *,
        datasets: list[str] | None = None,
        session_id: str = "",
        top_k: int = 8,
    ) -> str:
        return str(
            CogneeBackend.shared()
            .recall(query, datasets=datasets, session_id=session_id, top_k=top_k)
            .get("text")
            or ""
        )

    @classmethod
    def improve_sessions(
        cls,
        *,
        dataset: str = "",
        datasets: list[str] | None = None,
        all_datasets: bool = False,
        session_ids: list[str] | None = None,
        run_in_background: bool = True,
    ) -> dict[str, Any]:
        backend = CogneeBackend.shared()
        if all_datasets:
            rows = backend.datasets().get("datasets") or []
            names = [
                str(r.get("name") or "").strip()
                for r in rows
                if isinstance(r, dict) and str(r.get("name") or "").strip()
            ]
            if not names:
                names = ["peon"]
            return backend.improve_many(
                datasets=names,
                session_ids=session_ids,
                run_in_background=run_in_background,
            )
        many = [str(d).strip() for d in (datasets or []) if str(d).strip()]
        if many:
            return backend.improve_many(
                datasets=many,
                session_ids=session_ids,
                run_in_background=run_in_background,
            )
        return backend.improve(
            dataset=str(dataset or "peon").strip() or "peon",
            session_ids=session_ids,
            run_in_background=run_in_background,
        )

    @classmethod
    def datasets(cls) -> dict[str, Any]:
        return CogneeBackend.shared().datasets()

    @classmethod
    def sessions(cls, **kwargs: Any) -> dict[str, Any]:
        return CogneeBackend.shared().sessions(**kwargs)

    # --- Compatibility stubs (CrewAI memory removed) ---

    @staticmethod
    def remember_into(target: Any, content: str, meta: dict[str, Any] | None = None) -> bool:
        """Legacy hook: map into peon/project Cognee remember."""
        del target
        meta = meta or {}
        project_id = str(meta.get("project_id") or "")
        text = (content or "").strip()
        if not text:
            return False
        if project_id:
            doc = {
                "kind": str(meta.get("category") or "method"),
                "summary": text[:8000],
                "event": str(meta.get("event") or ""),
                "role_id": str(meta.get("role_id") or ""),
                "project_id": project_id,
                "indications": [str(meta.get("category") or "method")],
                "meta": {k: v for k, v in meta.items() if k not in {"category"}},
            }
            return bool(
                CogneeBackend.shared()
                .remember(
                    doc,
                    dataset_name=project_dataset(project_id),
                    self_improvement=False,
                )
                .get("ok")
            )
        return remember_peon(
            text,
            category=str(meta.get("category") or "digest"),
            event=str(meta.get("event") or ""),
            project_id=str(meta.get("project_id") or ""),
        )
