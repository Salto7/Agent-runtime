"""Engagement transcript records for CrewAI memory + role deduction digests.

Producers emit structured records (never scrape free-text logs). Peon persists
and mirrors into CrewAI Memory; Learn / propose_work consume digests.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

CATEGORIES: frozenset[str] = frozenset(
    {"action", "method", "outcome", "error", "replan", "handoff", "gap", "checkpoint"}
)


@dataclass(frozen=True)
class TranscriptRecord:
    """One structured engagement transcript row."""

    category: str
    summary: str
    project_id: str = ""
    objective_id: str = ""
    job_id: str = ""
    role_id: str = ""
    event: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def __post_init__(self) -> None:
        cat = (self.category or "").strip().lower()
        if cat not in CATEGORIES:
            object.__setattr__(self, "category", "action")
        if not self.created_at:
            object.__setattr__(
                self,
                "created_at",
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def record_from_event(
    *,
    category: str,
    summary: str,
    project_id: str = "",
    objective_id: str = "",
    job_id: str = "",
    role_id: str = "",
    event: str = "",
    payload: dict[str, Any] | None = None,
) -> TranscriptRecord:
    return TranscriptRecord(
        category=category,
        summary=(summary or "").strip()[:2000],
        project_id=str(project_id or ""),
        objective_id=str(objective_id or ""),
        job_id=str(job_id or ""),
        role_id=str(role_id or ""),
        event=str(event or ""),
        payload=dict(payload or {}),
    )


def append_jsonl(path: Path, record: TranscriptRecord) -> None:
    """Append one record to a transcript jsonl file (best-effort)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")


def read_jsonl(
    path: Path,
    *,
    categories: Iterable[str] | None = None,
    limit: int = 200,
) -> list[TranscriptRecord]:
    if not path.is_file():
        return []
    allow = {c.strip().lower() for c in (categories or ()) if str(c).strip()}
    rows: list[TranscriptRecord] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        cat = str(raw.get("category") or "").strip().lower()
        if allow and cat not in allow:
            continue
        rows.append(
            TranscriptRecord(
                category=cat or "action",
                summary=str(raw.get("summary") or ""),
                project_id=str(raw.get("project_id") or ""),
                objective_id=str(raw.get("objective_id") or ""),
                job_id=str(raw.get("job_id") or ""),
                role_id=str(raw.get("role_id") or ""),
                event=str(raw.get("event") or ""),
                payload=raw.get("payload")
                if isinstance(raw.get("payload"), dict)
                else {},
                created_at=str(raw.get("created_at") or ""),
            )
        )
    if limit > 0:
        rows = rows[-limit:]
    return rows


def digest_markdown(records: list[TranscriptRecord], *, limit: int = 80) -> str:
    """Human/LLM digest for propose_work and Learn role drafting."""
    if not records:
        return "(no transcript records)"
    lines: list[str] = ["## Engagement transcript digest"]
    for rec in records[-max(1, limit) :]:
        extra = ""
        if rec.payload:
            # Prefer structured keys without dumping huge blobs.
            keys = []
            for k in (
                "cli",
                "command",
                "link",
                "role_id",
                "job_id",
                "status",
                "skill_ids",
            ):
                if k in rec.payload and rec.payload[k] not in (None, "", []):
                    val = rec.payload[k]
                    if isinstance(val, (list, tuple)):
                        val = ",".join(str(x) for x in val[:8])
                    keys.append(f"{k}={str(val)[:120]}")
            if keys:
                extra = " (" + ", ".join(keys) + ")"
        lines.append(
            f"- [{rec.category}] {rec.summary[:300]}{extra}"
            + (f" @{rec.role_id}" if rec.role_id else "")
        )
    return "\n".join(lines)
