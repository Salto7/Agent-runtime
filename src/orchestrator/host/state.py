"""Control-plane state: DTOs and the ``StateStore`` contract.

``LocalControlPlane`` keeps *everything* it knows about a run (jobs, objectives, findings,
operator prompts, the asset graph, live guidance) behind a ``StateStore``. The in-memory
store is the default for a single process; ``orchestrator.queue.RedisStateStore`` shares the
same state between a web/CLI process and Dramatiq workers.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

TERMINAL = frozenset({"completed", "failed", "cancelled"})
OBJECTIVE_STATUSES = frozenset({"pending", "in_progress", "completed", "blocked", "cancelled"})


@dataclass
class Roe:
    """Rules of engagement for one project. ``None`` on the project = unrestricted.

    Entries are plain strings or ``{"type", "value"}`` dicts (``orchestrator.engagement.target_shapes``
    normalizes either). ``candidates`` are discovered-but-not-authorized targets awaiting promotion.
    """

    in_scope: list[Any] = field(default_factory=list)
    exclusions: list[Any] = field(default_factory=list)
    seed: list[Any] = field(default_factory=list)
    candidates: list[Any] = field(default_factory=list)
    authorization_note: str = ""
    testing_window_notes: str = ""
    abort_triggers: str = ""

    def summary(self) -> str:
        from orchestrator.utils.paths import format_roe_block

        return format_roe_block(self)

    def to_dict(self) -> dict[str, Any]:
        return {k: (list(v) if isinstance(v, list) else v) for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Roe | None":
        if data is None:
            return None
        known = cls.__dataclass_fields__  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Objective:
    """One planned unit of work. Released when every ``depends_on`` (sequence numbers) is completed."""

    seq: int
    title: str
    status: str = "pending"  # pending | in_progress | completed | blocked | cancelled
    role_id: str = ""
    note: str = ""  # blocked/cancel reason
    description: str = ""
    acceptance: str = ""
    phase: str = ""
    skill_ids: list[str] = field(default_factory=list)
    depends_on: list[int] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)


@dataclass
class JobState:
    id: str
    project_id: str
    title: str = ""
    description: str = ""
    role_ids: list[str] = field(default_factory=list)
    skill_ids: list[str] = field(default_factory=list)
    parent_id: str = ""
    objective_seq: int = 0
    depth: int = 1
    status: str = "pending"  # pending | running | idle | completed | failed | cancelled
    result: str = ""
    error: str = ""
    idle_note: str = ""
    extras: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    #: Last liveness beat from the worker running the job (0 = never); see ``LocalControlPlane.reap_stale``.
    heartbeat: float = 0.0
    #: ``""`` for ordinary jobs; ``"governance"`` for the project's resident governance PM.
    profile: str = ""
    #: Operator control request for a *running* job: ``""`` | ``"pause"`` | ``"kill"`` (cooperative; see plane.pause).
    control: str = ""
    #: Loaded on demand (``get_job(..., events=True)``); not part of the persisted job row.
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Schedule:
    """A durable-project schedule: fire a job (or re-run an objective) on a cron / interval / once."""

    id: str
    project_id: str
    title: str = ""
    role_id: str = ""
    brief: str = ""
    kind: str = "interval"  # once | interval | cron
    cron: str = ""
    interval_seconds: int = 0
    objective_seq: int = 0  # 0 = no objective: a standalone role job
    enabled: bool = True
    skip_if_running: bool = True
    next_run_at: float = 0.0  # epoch seconds; 0 = not armed
    last_run_at: float = 0.0
    last_job_id: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class ProjectMeta:
    id: str
    title: str = ""
    roe: Roe | None = None
    summary: str = ""  # the operator's brief; replans start from it
    kind: str = "timed"  # timed (finite) | durable (continuous, driven by schedules)
    plan_accepted: bool = True  # a durable plan must be accepted before objectives start
    status: str = "active"  # active | paused | stopped — only "active" projects release work / fire schedules


@runtime_checkable
class StateStore(Protocol):
    """Everything the control plane persists. Implementations must be safe for concurrent use."""

    # coordination
    def lock(self, name: str, *, ttl: float = 30.0, wait: float = 30.0): ...  # context manager: cross-process mutual exclusion by name
    # projects
    def get_project(self, project_id: str) -> ProjectMeta: ...
    def set_project(
        self, project_id: str, *, title: str = "", roe: Roe | None = None, summary: str = "",
        kind: str = "", plan_accepted: bool | None = None, status: str = "",
    ) -> ProjectMeta: ...
    # asset graph (opaque JSON blob owned by orchestrator.engagement.asset_graph)
    def get_graph(self, project_id: str) -> dict[str, Any] | None: ...
    def put_graph(self, project_id: str, graph: dict[str, Any]) -> None: ...
    # schedules
    def put_schedule(self, schedule: Schedule) -> None: ...
    def get_schedule(self, schedule_id: str) -> Schedule | None: ...
    def list_schedules(self, project_id: str = "") -> list[Schedule]: ...  # "" = every project
    def patch_schedule(self, schedule_id: str, **fields: Any) -> None: ...
    def delete_schedule(self, schedule_id: str) -> bool: ...
    # jobs
    def put_job(self, job: JobState) -> None: ...
    def get_job(self, job_id: str, *, events: bool = False) -> JobState | None: ...
    def list_jobs(self, project_id: str) -> list[JobState]: ...
    def list_projects(self) -> list[str]: ...
    def patch_job(self, job_id: str, **fields: Any) -> None: ...
    def claim_job(self, job_id: str, from_statuses: set[str], to_status: str) -> bool: ...
    def add_guidance(self, job_id: str, text: str) -> None: ...
    def pop_guidance(self, job_id: str) -> list[str]: ...
    def has_guidance(self, job_id: str) -> bool: ...
    def add_event(self, job_id: str, event: dict[str, Any]) -> None: ...
    def add_checkpoint(self, job_id: str, checkpoint: dict[str, Any]) -> int: ...
    def latest_checkpoint(self, job_id: str) -> dict[str, Any] | None: ...
    # objectives
    def get_objectives(self, project_id: str) -> list[Objective]: ...
    def set_objectives(self, project_id: str, objectives: list[Objective]) -> None: ...
    def patch_objective(self, project_id: str, seq: int, *, status: str, note: str = "") -> bool: ...
    # findings / prompts / assets
    def add_finding(self, project_id: str, row: dict[str, Any]) -> dict[str, Any] | None: ...
    def get_findings(self, project_id: str) -> list[dict[str, Any]]: ...
    def add_prompt(self, project_id: str, prompt: dict[str, Any]) -> None: ...
    def get_prompts(self, project_id: str) -> list[dict[str, Any]]: ...
    def set_prompt_answer(self, project_id: str, prompt_id: str, answer: str) -> dict[str, Any] | None: ...


def _copy_objective(o: "Objective") -> "Objective":
    return Objective(**{**o.__dict__, "skill_ids": list(o.skill_ids), "depends_on": list(o.depends_on),
                        "commands": list(o.commands)})


def finding_key(row: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    """Dedupe identity, plus the subjects: the same title on
    two different URLs / hashes is two findings."""
    subjects = sorted(
        f"{str(s.get('type') or '')}:{str(s.get('value') or '').lower()}"
        for s in (row.get("subjects") or [])
        if isinstance(s, dict) and s.get("type") not in {"host", "fqdn", "ip", "service", "netblock"}  # host/port are in the key
    )
    return (
        str(row.get("title") or "").lower(),
        str(row.get("host") or "").lower(),
        str(row.get("port") or ""),
        str(row.get("kind") or ""),
        str(row.get("evidence_path") or ""),
        "|".join(subjects),
    )


class MemoryStateStore:
    """Single-process store (default). ``get_job`` returns copies, like a remote store would."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._projects: dict[str, ProjectMeta] = {}
        self._jobs: dict[str, JobState] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._guidance: dict[str, list[str]] = {}
        self._checkpoints: dict[str, list[dict[str, Any]]] = {}
        self._objectives: dict[str, list[Objective]] = {}
        self._findings: dict[str, list[dict[str, Any]]] = {}
        self._prompts: dict[str, list[dict[str, Any]]] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._schedules: dict[str, Schedule] = {}
        self._graphs: dict[str, dict[str, Any]] = {}

    @contextmanager
    def lock(self, name: str, *, ttl: float = 30.0, wait: float = 30.0):
        with self._lock:
            lk = self._locks.setdefault(name, threading.RLock())
        with lk:
            yield

    def get_project(self, project_id: str) -> ProjectMeta:
        with self._lock:
            return self._projects.setdefault(project_id, ProjectMeta(project_id))

    def set_project(
        self, project_id: str, *, title: str = "", roe: Roe | None = None, summary: str = "",
        kind: str = "", plan_accepted: bool | None = None, status: str = "",
    ) -> ProjectMeta:
        with self._lock:
            meta = self.get_project(project_id)
            if title:
                meta.title = title
            if summary:
                meta.summary = summary
            if kind:
                meta.kind = kind
            if plan_accepted is not None:
                meta.plan_accepted = bool(plan_accepted)
            if status:
                meta.status = status
            if roe is not None:
                meta.roe = roe
            return meta

    def get_graph(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            g = self._graphs.get(project_id)
            return None if g is None else {"assets": [dict(a) for a in g.get("assets", [])],
                                            "relations": [dict(r) for r in g.get("relations", [])],
                                            "updated_at": g.get("updated_at", "")}

    def put_graph(self, project_id: str, graph: dict[str, Any]) -> None:
        with self._lock:
            self._graphs[project_id] = {"assets": [dict(a) for a in graph.get("assets", [])],
                                        "relations": [dict(r) for r in graph.get("relations", [])],
                                        "updated_at": graph.get("updated_at", "")}

    def put_schedule(self, schedule: Schedule) -> None:
        with self._lock:
            self._schedules[schedule.id] = Schedule(**schedule.__dict__)

    def get_schedule(self, schedule_id: str) -> Schedule | None:
        with self._lock:
            s = self._schedules.get(schedule_id)
            return Schedule(**s.__dict__) if s else None

    def list_schedules(self, project_id: str = "") -> list[Schedule]:
        with self._lock:
            rows = [Schedule(**s.__dict__) for s in self._schedules.values()
                    if not project_id or s.project_id == project_id]
        return sorted(rows, key=lambda r: (r.next_run_at or float("inf"), r.created_at))

    def patch_schedule(self, schedule_id: str, **fields: Any) -> None:
        with self._lock:
            s = self._schedules.get(schedule_id)
            if s is not None:
                for k, v in fields.items():
                    setattr(s, k, v)

    def delete_schedule(self, schedule_id: str) -> bool:
        with self._lock:
            return self._schedules.pop(schedule_id, None) is not None

    def put_job(self, job: JobState) -> None:
        with self._lock:
            self._jobs[job.id] = JobState(**{**job.__dict__, "events": []})

    def get_job(self, job_id: str, *, events: bool = False) -> JobState | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return JobState(**{**job.__dict__, "events": list(self._events.get(job_id, [])) if events else []})

    def list_projects(self) -> list[str]:
        with self._lock:
            return sorted({j.project_id for j in self._jobs.values()})

    def list_jobs(self, project_id: str) -> list[JobState]:
        with self._lock:
            rows = [j for j in self._jobs.values() if j.project_id == project_id]
        rows.sort(key=lambda j: j.created_at)
        return [JobState(**{**j.__dict__, "events": []}) for j in rows]

    def patch_job(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                for key, value in fields.items():
                    setattr(job, key, value)

    def claim_job(self, job_id: str, from_statuses: set[str], to_status: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in from_statuses:
                return False
            job.status = to_status
            return True

    def add_guidance(self, job_id: str, text: str) -> None:
        with self._lock:
            self._guidance.setdefault(job_id, []).append(text)

    def pop_guidance(self, job_id: str) -> list[str]:
        with self._lock:
            return self._guidance.pop(job_id, [])

    def has_guidance(self, job_id: str) -> bool:
        with self._lock:
            return bool(self._guidance.get(job_id))

    def add_event(self, job_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            self._events.setdefault(job_id, []).append(event)

    def add_checkpoint(self, job_id: str, checkpoint: dict[str, Any]) -> int:
        with self._lock:
            rows = self._checkpoints.setdefault(job_id, [])
            rows.append({**checkpoint, "seq": len(rows) + 1})
            return len(rows)

    def latest_checkpoint(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            rows = self._checkpoints.get(job_id)
            return dict(rows[-1]) if rows else None

    def get_objectives(self, project_id: str) -> list[Objective]:
        with self._lock:
            return [_copy_objective(o) for o in self._objectives.get(project_id, [])]

    def set_objectives(self, project_id: str, objectives: list[Objective]) -> None:
        with self._lock:
            self._objectives[project_id] = [_copy_objective(o) for o in objectives]

    def patch_objective(self, project_id: str, seq: int, *, status: str, note: str = "") -> bool:
        with self._lock:
            for o in self._objectives.get(project_id, []):
                if o.seq == seq:
                    o.status, o.note = status, note
                    return True
        return False


    def add_finding(self, project_id: str, row: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            rows = self._findings.setdefault(project_id, [])
            if any(finding_key(r) == finding_key(row) for r in rows):
                return None
            row = {**row, "seq": len(rows) + 1}
            rows.append(row)
            return row

    def get_findings(self, project_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._findings.get(project_id, []))

    def add_prompt(self, project_id: str, prompt: dict[str, Any]) -> None:
        with self._lock:
            self._prompts.setdefault(project_id, []).append(prompt)

    def get_prompts(self, project_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._prompts.get(project_id, []))

    def set_prompt_answer(self, project_id: str, prompt_id: str, answer: str) -> dict[str, Any] | None:
        with self._lock:
            for p in self._prompts.get(project_id, []):
                if p["id"] == prompt_id:
                    p["answer"] = answer
                    return dict(p)
        return None

