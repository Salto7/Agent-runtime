"""Append finding records for the peon worker to ingest (no Django).

Engagement discoveries about any subject class (with evidence). Peon
re-normalizes on ingest — this queue only requires a title. Do not queue
job/objective/agent status here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from workspace import WorkspaceStore


class FindingQueue:
    """NDJSON appender under ``workspace/findings_queue.jsonl``."""

    relative = "workspace/findings_queue.jsonl"

    def __init__(self, store: WorkspaceStore | None = None) -> None:
        self._store = store or WorkspaceStore()

    @property
    def path(self):
        path = self._store.path() / self.relative
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def append(self, **fields: Any) -> dict[str, Any] | None:
        title = str(fields.get("title") or "").strip()
        if not title:
            return None
        row = dict(fields)
        row["title"] = title[:255]
        row.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        return row

    def append_many(self, rows: list[dict[str, Any]]) -> int:
        n = 0
        for raw in rows or []:
            if isinstance(raw, dict) and self.append(**raw) is not None:
                n += 1
        return n


def record_finding(**fields: Any) -> dict[str, Any] | None:
    return FindingQueue().append(**fields)


def record_findings(rows: list[dict[str, Any]]) -> int:
    return FindingQueue().append_many(rows)
