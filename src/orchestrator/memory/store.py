"""Agent memory: typed, named, always-indexed notes (Claude-style) over Cognee.

* ``MemoryEntry`` rows are the source of truth, so entries can be updated,
  forgotten and listed exactly. Cognee receives a mirrored document for graph
  recall and the dreaming/improve cycle.
* Every agent starts with a compact **index** of what is known (one line per
  entry); details come back through ``recall_memory``. The engagement manager
  also gets the full text of operator feedback and decisions.
* Two scopes: ``project`` (one engagement — may be target specific) and
  ``platform`` (cross-engagement lessons — must be generic; guarded against
  leaking target data). ``project_id=None`` addresses the platform scope.
* Retrieval is BM25 (``rank-bm25``) over the local entries, merged with a
  Cognee graph completion when the service is reachable.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from rank_bm25 import BM25Okapi

from orchestrator.memory.models import MemoryEntry

logger = logging.getLogger(__name__)

KINDS = ("lesson", "feedback", "fact", "decision", "reference")
PLATFORM_KINDS = frozenset({"lesson", "reference"})
MAX_BODY = 4_000
MAX_DESCRIPTION = 240
INDEX_PROJECT_LIMIT = 30
INDEX_PLATFORM_LIMIT = 8
MANAGER_FULL_CHARS = 3_500

_WORD = re.compile(r"[a-z0-9_]{2,}")
_SLUG = re.compile(r"[^a-z0-9]+")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_URL = re.compile(r"https?://\S+", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid(project_id: object) -> str:
    return str(project_id or "").strip()


def entry_updated(entry: MemoryEntry) -> datetime:
    try:
        return datetime.fromisoformat(entry.updated_at)
    except (TypeError, ValueError):
        return datetime.fromtimestamp(0, timezone.utc)


def slugify(name: str) -> str:
    return _SLUG.sub("-", (name or "").strip().lower()).strip("-")[:64]


def _tokens(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def _scope(project_id: str | None):
    """Active entries of one scope (platform when ``project_id`` is None/empty)."""
    return MemoryEntry.objects.filter(project_id=_pid(project_id), status="active")


def target_specific_reason(text: str, scope_values: Iterable[str] = ()) -> str:
    """Why ``text`` must not become a platform-wide lesson ('' when it is fine).

    ``scope_values`` are the in-scope target strings of the engagement the note came from.
    """
    blob = text or ""
    if _IPV4.search(blob):
        return "contains an IP address"
    if _EMAIL.search(blob):
        return "contains an email address"
    if _URL.search(blob):
        return "contains a URL"
    low = blob.lower()
    for value in scope_values:
        v = str(value or "").strip().lower()
        forms = {v}
        host = v.split("://", 1)[-1].split("/", 1)[0].lstrip("*.")   # scheme, path and wildcard are not the target
        forms.add(host)
        forms.add(host.rsplit(":", 1)[0] if host.count(":") == 1 else host)   # …nor is the port
        for form in forms:
            if len(form) >= 4 and form in low:
                return f"mentions in-scope target {form!r}"
    return ""


# ── write path ───────────────────────────────────────────────────────────


def save(
    project_id: str | None,
    *,
    name: str,
    kind: str,
    description: str,
    body: str = "",
    role_id: str = "",
    job_id: str = "",
    created_by: str = "",
    scope_values: Iterable[str] = (),
) -> tuple[MemoryEntry, bool]:
    """Create or update a memory. Returns ``(entry, created)``; raises ``ValueError``."""
    pid = _pid(project_id)
    slug = slugify(name)
    if not slug:
        raise ValueError("name is required")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    desc = " ".join((description or "").split())[:MAX_DESCRIPTION]
    if not desc:
        raise ValueError("description (one-line hook) is required")
    text = (body or "").strip()[:MAX_BODY]
    if not pid:
        if kind not in PLATFORM_KINDS:
            raise ValueError(
                "platform memory is for generic lessons/references only; "
                f"save {kind!r} entries in the project scope"
            )
        reason = target_specific_reason(f"{desc}\n{text}", scope_values)
        if reason:
            raise ValueError(
                f"platform lessons must be generic ({reason}); "
                "generalise it or save it in the project scope"
            )

    existing = MemoryEntry.objects.filter(project_id=pid, name=slug).first()
    if existing is not None:
        existing.kind, existing.description, existing.body = kind, desc, text
        existing.status = "active"
        if role_id:
            existing.role_id = role_id[:64]
        existing.save()
        entry, created = existing, False
    else:
        entry = MemoryEntry(
            project_id=pid,
            name=slug,
            kind=kind,
            description=desc,
            body=text,
            role_id=(role_id or "")[:64],
            created_by=(created_by or "")[:96],
            source_job_id=str(job_id or "") if pid else "",
            created_at=_now(),
        )
        entry.save(force_insert=True)
        created = True
    _mirror(entry, pid)
    return entry, created


def forget(project_id: str | None, name: str) -> bool:
    """Archive a memory (it leaves the index and recall)."""
    pid = _pid(project_id)
    slug = slugify(name)
    n = MemoryEntry.objects.filter(project_id=pid, name=slug).update(status="archived")
    if n:
        _mirror_tombstone(pid, slug)
    return bool(n)


def record_feedback(project_id: str, text: str, *, source: str = "chat") -> MemoryEntry | None:
    """Capture operator guidance as durable feedback (deduplicated)."""
    body = " ".join((text or "").split())
    if len(body) < 12 or body.endswith("?"):
        return None
    digest = hashlib.sha1(body.lower().encode()).hexdigest()[:8]
    first = re.split(r"(?<=[.!?])\s", body, maxsplit=1)[0]
    try:
        entry, _ = save(
            project_id,
            name=f"feedback-{digest}",
            kind="feedback",
            description=first[:200],
            body=(
                f"{text.strip()}\n\n"
                f"**Why:** instruction from the operator ({source}).\n"
                "**How to apply:** honor this in all later work on this engagement "
                "unless the operator revises it; it overrides your own preference."
            ),
            created_by="operator",
        )
        return entry
    except ValueError:
        return None


def record_project_digest(
    project_id: str,
    status: str,
    *,
    objectives: Iterable[tuple[str, str]] = (),
    findings: Iterable[tuple[str, str, str]] = (),
) -> MemoryEntry | None:
    """Auto-capture a project-scope summary when an engagement reaches a terminal status.

    ``objectives`` are ``(title, status)``; ``findings`` are ``(title, severity, kind)``.
    One row per project (re-run updates it in place).
    """
    objectives = list(objectives)
    findings = list(findings)
    if not objectives and not findings:
        return None

    sev_counts: dict[str, int] = {}
    for _, sev, _ in findings:
        sev_counts[sev] = sev_counts.get(sev, 0) + 1
    sev_line = ", ".join(f"{n} {s}" for s, n in sorted(sev_counts.items())) or "none"
    obj_lines = "\n".join(f"- [{s}] {t}" for t, s in objectives) or "_none_"
    finding_lines = (
        "\n".join(
            f"- [{sev}] {t}" + (f" ({kind})" if kind else "") for t, sev, kind in findings[:50]
        )
        or "_none_"
    )
    desc = f"Engagement finished ({status}): {len(objectives)} objective(s), findings: {sev_line}."
    body = (
        f"**Status:** {status}\n\n"
        f"**Objectives:**\n{obj_lines}\n\n"
        f"**Findings ({sev_line}):**\n{finding_lines}\n\n"
        "**Why:** auto-captured project digest so this engagement's outcome is "
        "recorded even when no agent called save_memory.\n"
        "**How to apply:** read before replanning or starting a similar "
        "engagement against the same scope."
    )
    try:
        entry, _ = save(
            project_id,
            name="project-digest",
            kind="fact",
            description=desc[:MAX_DESCRIPTION],
            body=body,
            created_by="system",
        )
        return entry
    except ValueError:
        logger.warning("record_project_digest failed for project=%s", project_id)
        return None


# ── Cognee mirror ────────────────────────────────────────────────────────


def _dataset(project_id: str) -> str:
    from orchestrator.crew.memory import project_dataset

    return project_dataset(project_id) if project_id else "platform"


def _mirror(entry: MemoryEntry, project_id: str) -> None:
    if not project_id:
        # The platform dataset is rebuilt as a whole by ``sync_platform_dataset``
        # (idempotent; no stale versions of an edited note linger).
        return
    try:
        from orchestrator.crew.cognee_backend import CogneeBackend
        from orchestrator.crew.memory import memory_enabled

        if not memory_enabled():
            return
        CogneeBackend.shared().remember(
            {
                "kind": "memory",
                "memory_kind": entry.kind,
                "name": entry.name,
                "summary": entry.description,
                "body": entry.body,
                "role_id": entry.role_id,
                "project_id": project_id,
                "status": "active",
                "indications": ["memory", entry.kind],
            },
            dataset_name=_dataset(project_id),
            session_id="memory",
            self_improvement=False,
            run_in_background=True,
        )
    except Exception:  # noqa: BLE001 — Cognee is an accelerator, never a gate
        logger.debug("memory mirror failed", exc_info=True)


def sync_platform_dataset() -> dict[str, Any]:
    """Rebuild the ``platform`` Cognee dataset from the platform memory entries.

    The memory table is the source of truth; Cognee is a retrieval index over
    it. A full, idempotent rebuild keeps the index exactly equal to the records
    (edits replace, retired notes disappear).
    """
    from orchestrator.crew.cognee_backend import CogneeBackend
    from orchestrator.crew.memory import memory_enabled, platform_memory_enabled

    if not (memory_enabled() and platform_memory_enabled()):
        return {"ok": False, "skipped": True}
    backend = CogneeBackend.shared()
    entries = list(_scope(None))
    removed = backend.clear_dataset("platform")
    sent = 0
    for e in entries:
        text = f"# {e.name} ({e.kind})\n{e.description}\n\n{(e.body or '').strip()}".strip()
        result = backend.remember(
            text,
            dataset_name="platform",
            session_id="",
            self_improvement=False,
            run_in_background=True,
            document_name=e.name,
        )
        sent += 1 if result.get("ok") else 0
    return {"ok": True, "removed": removed, "entries": len(entries), "sent": sent}


def _mirror_tombstone(project_id: str, slug: str) -> None:
    if not project_id:
        return  # platform dataset is rebuilt from the table; retired notes just drop out
    try:
        from orchestrator.crew.cognee_backend import CogneeBackend
        from orchestrator.crew.memory import memory_enabled

        if memory_enabled():
            CogneeBackend.shared().remember(
                {
                    "kind": "memory",
                    "name": slug,
                    "status": "archived",
                    "summary": f"Memory {slug!r} was retired; disregard earlier notes under this name.",
                },
                dataset_name=_dataset(project_id),
                session_id="memory",
                run_in_background=True,
            )
    except Exception:  # noqa: BLE001
        logger.debug("memory tombstone failed", exc_info=True)


# ── read path ────────────────────────────────────────────────────────────


def _entries(project_id: str | None) -> list[MemoryEntry]:
    out = list(_scope(project_id))
    if _pid(project_id):
        out += list(_scope(None))
    return out


def _mark_recalled(entries: list[MemoryEntry]) -> None:
    now = _now()
    for e in entries:
        try:
            MemoryEntry.objects.filter(project_id=e.project_id, name=e.name).update(
                recall_count=int(e.recall_count or 0) + 1, last_recalled_at=now
            )
        except Exception:  # noqa: BLE001
            logger.debug("mark recalled failed", exc_info=True)


def search(
    project_id: str | None,
    query: str,
    *,
    role_id: str = "",
    limit: int = 5,
    mark: bool = True,
) -> list[tuple[MemoryEntry, float]]:
    """Rank this engagement's and the platform's memories for ``query`` (BM25)."""
    entries = _entries(project_id)
    if not entries:
        return []
    docs = [
        _tokens(f"{e.name.replace('-', ' ')} {e.description} {e.body} {e.kind} {e.role_id}")
        for e in entries
    ]
    q = _tokens(query)
    scores = [0.0] * len(entries)
    if q and any(docs):
        raw = list(BM25Okapi([d or ["_"] for d in docs]).get_scores(q))
        # BM25 idf goes negative on small corpora (a term in half the docs): rank only the docs that actually
        # contain a query term, shifted to positive so the boosts below can only ever help.
        hits = [i for i, d in enumerate(docs) if set(d) & set(q)]
        if hits:
            floor = min(raw[i] for i in hits)
            for i in hits:
                scores[i] = float(raw[i]) - floor + 1.0
    ranked: list[tuple[MemoryEntry, float]] = []
    for e, s in zip(entries, scores):
        boost = 1.0
        if role_id and e.role_id == role_id:
            boost += 0.25
        if e.kind == "feedback":
            boost += 0.2  # operator guidance outranks incidental notes
        ranked.append((e, float(s) * boost))
    ranked.sort(key=lambda r: (r[1], entry_updated(r[0])), reverse=True)
    top = [r for r in ranked if r[1] > 0][:limit]
    if not top and not q:   # no query at all: the most recent notes, not arbitrary ones for a query that matched nothing
        top = ranked[: min(limit, 3)]
    if mark:
        _mark_recalled([e for e, _ in top])
    return top


def _fmt_entry(e: MemoryEntry, *, body_chars: int = 600) -> str:
    scope = "engagement" if e.project_id else "platform"
    head = f"- [{e.kind}] {e.name} ({scope}, updated {entry_updated(e):%Y-%m-%d}) — {e.description}"
    body = (e.body or "").strip()
    return head + (f"\n  {body[:body_chars]}" if body else "")


def recall_text(project_id: str | None, query: str, *, role_id: str = "") -> str:
    """Answer for the ``recall_memory`` tool."""
    hits = search(project_id, query, role_id=role_id, limit=5)
    parts: list[str] = []
    if hits:
        parts.append(
            "Stored memories (most relevant first):\n" + "\n".join(_fmt_entry(e) for e, _ in hits)
        )
    graph = ""
    try:
        from orchestrator.crew.memory import MemoryService, memory_enabled

        if memory_enabled() and (query or "").strip():
            datasets = ([_dataset(_pid(project_id))] if _pid(project_id) else []) + ["platform"]
            graph = (MemoryService.recall(query, datasets=datasets, top_k=4) or "").strip()
    except Exception:  # noqa: BLE001
        graph = ""
    if graph:
        parts.append("Related knowledge (Cognee graph recall):\n" + graph[:1800])
    if not parts:
        return "No relevant memory found."
    parts.append(
        "Memories are point-in-time notes — check them against the current "
        "state before relying on them, and `save_memory` to update one."
    )
    return "\n\n".join(parts)


def index_block(project_id: str, *, role_id: str = "", manager: bool = False) -> str:
    """The memory prelude every agent starts with."""
    mine = sorted(
        _scope(project_id),
        key=lambda e: (e.kind != "feedback", -int(e.recall_count or 0), -entry_updated(e).timestamp()),
    )
    plat = [
        e for e in _scope(None) if not role_id or not e.role_id or e.role_id == role_id
    ]
    # Consolidated playbooks outrank raw per-run gists in the index.
    plat.sort(
        key=lambda e: (
            not e.name.startswith("playbook-"),
            e.role_id != role_id,
            -int(e.recall_count or 0),
            -entry_updated(e).timestamp(),
        )
    )
    lines = [
        "## Memory",
        "Notes carried over from earlier work (point-in-time — verify before "
        "relying on them). `recall_memory` fetches detail, `save_memory` records "
        "something durable you learned, `forget_memory` retires a stale note.",
    ]
    if mine:
        lines.append("### This engagement")
        lines += [f"- [{e.kind}] {e.name} — {e.description}" for e in mine[:INDEX_PROJECT_LIMIT]]
        if len(mine) > INDEX_PROJECT_LIMIT:
            lines.append(f"- … {len(mine) - INDEX_PROJECT_LIMIT} more (use recall_memory)")
    if plat:
        lines.append("### Platform lessons")
        lines += [f"- [{e.kind}] {e.name} — {e.description}" for e in plat[:INDEX_PLATFORM_LIMIT]]
    if manager:
        full = [e for e in mine if e.kind in {"feedback", "decision"}]
        if full:
            lines.append("### Standing instructions & decisions (full text)")
            budget = MANAGER_FULL_CHARS
            for e in full:
                chunk = f"**{e.name}** ({e.kind}): {(e.body or e.description).strip()}"
                if budget <= 0:
                    break
                lines.append(chunk[:budget])
                budget -= len(chunk)
    if manager:
        recap = _bootstrap_recall(project_id)
        if recap:
            lines.append("### Recalled from earlier engagements (Cognee, unverified)")
            lines.append(recap)
    if not mine and not plat and not (manager and "### Recalled" in "\n".join(lines)):
        lines.append("(no memories yet — save durable discoveries as you work)")
    return "\n".join(lines)


def _bootstrap_recall(project_id: str, limit: int = 1_800) -> str:
    """Graph recall written when the project was created (``memory_bootstrap.md``)."""
    try:
        from orchestrator.crew.memory import bootstrap_path

        path = bootstrap_path(str(project_id))
        if path is None or not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ""
    text = text.split("\n", 2)[-1].strip() if text.startswith("# Memory bootstrap") else text.strip()
    return text[:limit]
