"""Finished-run records: what the dream cycle reads, and where they come from.

A *run* is one finished agent job. The host owns job storage (a DB, a queue …),
so dreaming never queries it directly: it asks a :class:`RunSource` for
:class:`RunRecord` rows. :class:`JsonRunLog` is the batteries-included source —
the local control plane writes one JSON file per finished run under the
project's workspace, and the same class reads them back.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable

from orchestrator.utils.paths import safe_workspace_key

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class RunRecord:
    """One finished (or in-flight) agent run, as the dream cycle sees it."""

    job_id: str
    project_id: str = ""
    objective_id: str = ""
    title: str = ""
    description: str = ""
    role_ids: list[str] = field(default_factory=list)
    skill_ids: list[str] = field(default_factory=list)
    status: str = "completed"
    error: str = ""
    objective_title: str = ""
    acceptance: str = ""
    #: ``{"message_type", "content", "metadata"}`` rows in order (the run's stream).
    events: list[dict[str, Any]] = field(default_factory=list)
    #: In-scope target strings — keeps target names out of platform-wide lessons.
    scope_values: list[str] = field(default_factory=list)
    updated_at: str = ""

    @property
    def id(self) -> str:  # ``job_session_id`` reads ``id`` / ``objective_id``
        return self.job_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunRecord":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@runtime_checkable
class RunSource(Protocol):
    def finished_runs(self, limit: int = 60) -> Iterable[RunRecord]:
        """Most recently updated runs first (terminal and running)."""
        ...


class JsonRunLog:
    """File-backed :class:`RunSource` (``<workspaces>/<project>/runs/<job>.json``)."""

    def __init__(self, workspaces_dir: Path | str | None = None) -> None:
        self._root = Path(workspaces_dir) if workspaces_dir else None

    @property
    def root(self) -> Path:
        if self._root is not None:
            return self._root
        from orchestrator.config import get_config

        return Path(get_config().workspaces_dir)

    def _dir(self, project_id: str) -> Path:
        return self.root / safe_workspace_key(project_id or "_") / "runs"

    def write(self, record: RunRecord) -> Path:
        record.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        path = self._dir(record.project_id) / f"{_SAFE.sub('-', record.job_id)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record.to_dict(), ensure_ascii=False), encoding="utf-8")
        return path

    def finished_runs(self, limit: int = 60) -> list[RunRecord]:
        rows: list[RunRecord] = []
        root = self.root
        if not root.is_dir():
            return rows
        for path in root.glob("*/runs/*.json"):
            try:
                rows.append(RunRecord.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError) as exc:
                logger.debug("skipping unreadable run log %s: %s", path, exc)
        rows.sort(key=lambda r: r.updated_at, reverse=True)
        return rows[: max(1, limit)]
