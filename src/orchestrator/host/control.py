"""In-process control plane: the stand-alone replacement for Peon's Django layer.

Peon's web app owns jobs, objectives, findings, operator prompts and A2A in a
database and runs jobs on a Dramatiq queue. This module provides the same
*contract* (``AgentBridgeBase`` for the agent, ``run_agent`` for execution) with
plain Python state and a thread pool, so any host can run crews without Django,
Redis or a queue:

* :class:`LocalControlPlane` — registers jobs, runs each in its own thread with
  the project sandbox bound, parks/wakes jobs on A2A messages, writes run logs.
* :class:`LocalAgentBridge` — the per-job ``AgentBridgeBase`` (spawn, wait, A2A,
  memory, findings, objectives, artifacts, RoE) backed by that state.

Everything that a host may want to own (operator prompts, RoE, job storage) is a
small hook or dataclass here; subclass or replace it, the agent code does not change.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_runtime.api import Session
from orchestrator.agent.bridge_base import AgentBridgeBase, AgentLink
from orchestrator.agent.config import AgentRunConfig
from orchestrator.agent.job import JobScope
from orchestrator.agent.runtime_base import AgentRunResult, run_agent
from orchestrator.agent.transcript import (
    TranscriptRecord,
    append_jsonl,
    digest_markdown,
    read_jsonl,
    record_from_event,
)
from orchestrator import settings
from orchestrator.brain.a2a import A2AHub
from orchestrator.config import get_config
from orchestrator.crew.runs import JsonRunLog, RunRecord
from orchestrator.host.checkpoints import resume_context_text, write_checkpoint
from orchestrator.host.scheduler import DEP_NOTE, ObjectiveScheduler
from orchestrator.host.governance import GovernanceManager
from orchestrator.host.schedules import ScheduleManager
from orchestrator.host.state import (
    OBJECTIVE_STATUSES,
    TERMINAL,
    JobState,
    MemoryStateStore,
    Objective,
    ProjectMeta,
    Roe,
    StateStore,
)
from orchestrator.utils.job_env import JobEnv
from orchestrator.utils.paths import ensure_workspace_layout, safe_join, safe_workspace_key
from orchestrator.utils.stream_events import (
    EVENT_READ_ARTIFACT,
    EVENT_RECORD_FINDING,
    EVENT_REPORT_NOTE,
    EVENT_WORK_FINISHED,
    EVENT_WRITE_ARTIFACT,
    envelope,
)

logger = logging.getLogger(__name__)

EventHook = Callable[[dict[str, Any]], None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _file_lock(path: Path):
    """Exclusive advisory lock, so concurrent worker processes cannot clobber the report."""
    import fcntl

    with open(path, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)





# ── bridge ───────────────────────────────────────────────────────────────


class LocalAgentBridge(AgentBridgeBase):
    """``AgentBridgeBase`` for one job, backed by :class:`LocalControlPlane` state."""

    def __init__(self, plane: "LocalControlPlane", job: JobState) -> None:
        self.plane = plane
        self.job = job
        self.idle_requested = False
        self.idle_note = ""

    # -- helpers ------------------------------------------------------------

    @property
    def project(self) -> ProjectMeta:
        return self.plane.store.get_project(self.job.project_id)

    @property
    def store(self) -> StateStore:
        return self.plane.store

    @property
    def workspace(self) -> Path:
        return self.plane.workspace(self.job.project_id)

    def _role_id(self) -> str:
        return (self.job.role_ids[0] if self.job.role_ids else "").strip()

    def interrupted(self) -> str:
        """Why this job must stop ('' when it may continue): an operator pause/kill, or cancellation."""
        cur = self.store.get_job(self.job.id)
        if cur is None:
            return ""
        if cur.status == "cancelled" or cur.control == "kill":
            return "this job was cancelled by the operator"
        if cur.control == "pause":
            return "this job was paused by the operator"
        return ""

    # -- stream ---------------------------------------------------------------

    def emit(
        self, message_type: str, content: str, *, metadata: dict[str, Any] | None = None
    ) -> None:
        event = {
            "job_id": self.job.id,
            "project_id": self.job.project_id,
            "message_type": message_type,
            "content": content or "",
            "metadata": dict(metadata or {}),
            "at": _now(),
        }
        self.store.add_event(self.job.id, event)
        self.plane.dispatch(event)

    # -- delegation -----------------------------------------------------------

    def spawn_agent(
        self,
        *,
        title: str,
        description: str,
        role_ids: list[str] | None = None,
        link: AgentLink = "child",
        scope_slice: dict[str, str] | None = None,
        skill_ids: list[str] | None = None,
    ) -> str:
        from orchestrator.crew.roles.registry import RoleRegistry

        kind = (link or "child").strip().lower() or "child"
        if kind not in {"peer", "child"}:
            return "Error: link must be 'peer' or 'child'"
        if (why := self.interrupted()):
            return f"Error: {why} — end your turn now."
        roles = [str(r).strip() for r in (role_ids or self.job.role_ids) if str(r).strip()]
        if not roles:
            return "Error: no role_ids on this job to inherit for spawn"
        reg = RoleRegistry.shared()
        for rid in roles:
            role = reg.get(rid)
            if role is None:
                return f"Error: unknown role_id {rid!r}"
            if role.is_manager:
                return f"Error: cannot spawn manager role {rid!r}"
        cfg = self.plane.agent_config
        brief = (description or "").strip()
        if scope_slice:
            lines = [f"- {k}: {v}" for k, v in scope_slice.items() if str(k).strip()]
            if lines:
                brief = (brief + "\n\n## Scope slice\n" + "\n".join(lines)).strip()
            for rid in roles:
                role = reg.get(rid)
                if role is not None and role.is_technical:  # a slice must stay inside RoE (PM/analyzer don't probe)
                    for val in scope_slice.values():
                        if (reason := self.assert_in_scope(str(val))):
                            return f"Error: scope_slice denied for {val!r}: {reason}"
        resolved = self._resolve_child_skills(roles, skill_ids)
        if isinstance(resolved, str):
            return resolved
        with self.store.lock(f"spawn:{self.job.project_id}"):
            live = [j for j in self.store.list_jobs(self.job.project_id) if j.status not in TERMINAL]
            if kind == "child":
                if self.job.depth >= cfg.max_subagent_depth:
                    return f"Error: child depth limit ({cfg.max_subagent_depth})"
                if len([j for j in live if j.parent_id == self.job.id]) >= cfg.max_subagents:
                    return f"Error: too many active child agents (max {cfg.max_subagents})"
            else:
                if not self.job.objective_seq:
                    return "Error: peer spawn requires an objective on this job."
                active = [j for j in live if j.objective_seq == self.job.objective_seq]
                if len(active) >= max(cfg.max_subagents + 1, 2):
                    return f"Error: too many active agents on this objective (max {cfg.max_subagents + 1})"
            if self.job.objective_seq:
                obj = next((o for o in self.store.get_objectives(self.job.project_id)
                            if o.seq == self.job.objective_seq), None)
                if obj is not None and obj.status in {"completed", "cancelled"}:
                    return ("Error: cannot add work to a terminal objective; reopen it through replan or a "
                            "durable schedule first")
            child = self.plane.submit(
                project_id=self.job.project_id,
                title=(title or ("peer" if kind == "peer" else "child-agent"))[:255],
                description=brief,
                role_ids=roles,
                skill_ids=resolved,
                parent_id=self.job.id if kind == "child" else self.job.parent_id,
                objective_seq=self.job.objective_seq,
                depth=self.job.depth + (1 if kind == "child" else 0),
            )
        self._record_handoff(child, link=kind, title=title, role_ids=roles, skill_ids=resolved)
        return child.id

    def _lane_skills(self, role) -> list[str]:
        """Skills this job's objective bound it to — the lane its workers stay in."""
        from orchestrator.skills.bind import resolve_skill_ids

        raw = [str(s).strip() for s in (self.job.skill_ids or []) if str(s).strip()]
        if not raw and self.job.objective_seq:
            obj = next((o for o in self.store.get_objectives(self.job.project_id)
                        if o.seq == self.job.objective_seq), None)
            raw = [str(s).strip() for s in (obj.skill_ids if obj else []) if str(s).strip()]
        return list(resolve_skill_ids(role, raw)) if raw else []

    @staticmethod
    def _canonical_skill_id(requested: str, known: list[str]) -> str:
        """Map a loosely written skill id onto a known one ('' when nothing/ambiguous matches)."""
        want = requested.strip().lower()
        for sid in known:
            if sid.lower() == want:
                return sid
        hits = [sid for sid in known if sid.lower().rsplit("/", 1)[-1] == want]
        return hits[0] if len(hits) == 1 else ""

    def _resolve_child_skills(self, roles: list[str], skill_ids: list[str] | None) -> "list[str] | str":
        """Bind child skills; error string on failure.

        A worker for the *same role* stays inside this job's lane (the skills its objective was planned with):
        omitting ``skill_ids`` inherits the lane, naming one outside it is refused. Another role is bound by
        that role's own skill group.
        """
        from orchestrator.crew.roles.registry import RoleRegistry
        from orchestrator.skills.bind import resolve_skill_ids

        primary = roles[0] if roles else ""
        role = RoleRegistry.shared().get(primary)
        if role is None:
            return f"Error: unknown role_id {primary!r}"
        own_role = (self.job.role_ids[0] if self.job.role_ids else "").strip()
        lane = self._lane_skills(role) if primary == own_role else []
        group = list(resolve_skill_ids(role, None))
        requested = [str(s).strip() for s in (skill_ids or []) if str(s).strip()]
        if not requested:
            return lane or list(role.skills or ()) or group
        known = list(dict.fromkeys([*lane, *group, *(role.skills or ())]))
        selected: list[str] = []
        unknown: list[str] = []
        for want in requested:
            sid = self._canonical_skill_id(want, known)
            if sid and sid not in selected:
                selected.append(sid)
            elif not sid:
                unknown.append(want)
        if unknown or not selected:
            return (f"Error: unknown skill_ids {', '.join(unknown or requested)} for role {primary!r}; "
                    f"available: {', '.join(known) or '(none)'}")
        usable = list(resolve_skill_ids(role, selected))
        if not usable:
            return f"Error: no valid skill_ids for role {primary!r} (requested: {', '.join(requested)})"
        if lane:
            outside = [sid for sid in usable if sid not in lane]
            if outside:
                return (f"Error: {', '.join(outside)} is outside this objective's skills ({', '.join(lane)}). "
                        "Spawn with those skill_ids (or leave skill_ids empty) — other skills belong to other "
                        "objectives/roles.")
        return usable

    def _record_handoff(self, child: JobState, *, link: str, title: str, role_ids: list[str],
                        skill_ids: list[str]) -> None:
        """Delegation stream event + transcript record; keep the governor in the picture (inform, no wake)."""
        from orchestrator.agent.delegation import SpawnWorkRequest
        from orchestrator.utils.stream_events import EVENT_SPAWN_WORK

        role_id = role_ids[0] if role_ids else ""
        req = SpawnWorkRequest(title=(title or "")[:255], brief="", role_id=role_id, link=link,
                               skill_ids=tuple(s for s in skill_ids if s))
        payload = req.payload(job_id=child.id, parent_job_id=self.job.id)
        payload["objective_id"] = str(self.job.objective_seq or "")
        self.emit("log", f"Delegation: spawned {link} {child.id} ({role_id or '-'})",
                  metadata=envelope(EVENT_SPAWN_WORK, payload))
        self.append_transcript(record_from_event(
            category="handoff", summary=f"spawned {link} job for {(title or '')[:120]}",
            project_id=self.job.project_id, objective_id=str(self.job.objective_seq or ""),
            job_id=self.job.id, role_id=self._role_id(), event=EVENT_SPAWN_WORK, payload=payload))
        self.plane.governance.try_notify(
            self.job.project_id, from_job_id=self.job.id,
            text=f"{self._role_id() or 'agent'} spawned {link} agent {child.id} ({role_id or '-'}): {(title or '')[:160]}")

    def wait_agents(
        self, *, job_ids: list[str] | None = None, timeout_seconds: int = 600
    ) -> str:
        del timeout_seconds  # non-blocking by contract
        kids = [j for j in self.store.list_jobs(self.job.project_id) if j.parent_id == self.job.id]
        if job_ids:
            kids = [k for k in kids if k.id in set(job_ids)]
        if not kids:
            return "No child agents found for this job."
        done = [k for k in kids if k.status in TERMINAL]
        pending = [k for k in kids if k.status not in TERMINAL]
        if not pending:
            lines = [f"All {len(kids)} child agent(s) finished:"]
            lines += [f"- {k.title} ({k.id}) → {k.status}" for k in done]
            return "\n".join(lines)
        me = self.store.get_job(self.job.id)
        if me is not None and me.status == "cancelled":
            return "Parent job cancelled; stop waiting."
        lines = [
            f"{len(done)} finished, {len(pending)} still running "
            "(call wait_for_work again later; do not block):"
        ]
        lines += [f"- {k.title} ({k.id}) → {k.status}" for k in [*pending, *done]]
        return "\n".join(lines)

    def list_agents(self) -> str:
        peers = [
            j
            for j in self.store.list_jobs(self.job.project_id)
            if j.id != self.job.id and j.objective_seq == self.job.objective_seq
        ]
        rows = [
            f"{j.id} [{j.status}] {j.title} roles={','.join(j.role_ids) or '-'}" for j in peers[:40]
        ]
        return "\n".join(rows) if rows else "No other jobs on this objective."

    def list_delegations(self) -> str:
        jobs = self.store.list_jobs(self.job.project_id)
        if not jobs:
            return "No delegations yet."
        by_parent: dict[str, list[JobState]] = {}
        for j in jobs:
            by_parent.setdefault(j.parent_id, []).append(j)
        lines: list[str] = []

        def walk(parent: str, depth: int) -> None:
            for j in by_parent.get(parent, []):
                lines.append(
                    f"{'  ' * depth}- {j.title} ({j.id}) [{j.status}] roles={','.join(j.role_ids) or '-'}"
                )
                walk(j.id, depth + 1)

        walk("", 0)
        return "\n".join(lines)

    def propose_agents(self, *, context_notes: str = "", max_agents: int = 4) -> str:
        from orchestrator.agent.propose import propose_agents as propose_specs
        from orchestrator.crew.roles.catalog import catalog_role_ids
        from orchestrator.crew.roles.registry import RoleRegistry

        obj = next(
            (o for o in self.store.get_objectives(self.job.project_id) if o.seq == self.job.objective_seq),
            None,
        )
        primary = (obj.role_id.split(",")[0].strip() if obj and obj.role_id else "") or self._role_id()
        known = set(catalog_role_ids())
        allowed = [
            r.id
            for r in RoleRegistry.shared().list_roles()
            if not r.is_manager and (not known or r.id in known)
        ]
        notes = (context_notes or "").strip() or (
            f"Current job brief:\n{(self.job.description or '')[:2000]}"
        )
        try:
            from orchestrator.skills.role_memory import role_skills_prompt_block

            learned = role_skills_prompt_block()
            if learned:
                notes = (notes + "\n\n" + learned).strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("role_skills_prompt_block failed: %s", exc)
        specs = propose_specs(
            objective_title=obj.title if obj else self.job.title,
            objective_description=self.job.description,
            acceptance="",
            primary_role=primary,
            allowed_roles=allowed or ([primary] if primary else []),
            context_notes=notes,
            transcript_digest=self._transcript_digest(),
            max_agents=max_agents,
        )
        if not specs:
            return "No peer agents proposed (solo work is enough, or LLM unavailable)."
        created = []
        for spec in specs:
            msg = self.spawn_agent(
                title=spec.title,
                description=spec.description,
                role_ids=[spec.role_id] if spec.role_id else None,
                link="child",
            )
            created.append(f"- {msg} [{spec.role_id or primary}] {spec.title}")
        return "Spawned proposed work:\n" + "\n".join(created)

    # -- transcript -------------------------------------------------------------

    def _transcript_path(self) -> Path:
        return self.workspace / "transcript.jsonl"

    def append_transcript(self, record: Any) -> None:
        if not isinstance(record, TranscriptRecord):
            return
        try:
            append_jsonl(self._transcript_path(), record)
        except Exception:  # noqa: BLE001
            return
        try:
            from orchestrator.crew.memory import MemoryService

            MemoryService.remember_transcript(
                type("J", (), {"project_id": self.job.project_id, "id": self.job.id,
                               "objective_id": str(self.job.objective_seq or "")})(),
                record,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("remember_transcript failed: %s", exc)

    def _transcript_digest(
        self,
        categories: tuple[str, ...] = ("error", "handoff", "gap", "replan", "outcome"),
        limit: int = 80,
    ) -> str:
        try:
            rows = read_jsonl(self._transcript_path(), categories=categories, limit=limit)
        except Exception:  # noqa: BLE001
            return ""
        return digest_markdown(rows, limit=min(limit, 60))

    # -- objectives -------------------------------------------------------------

    def list_objectives(self) -> str:
        rows = self.store.get_objectives(self.job.project_id)
        if not rows:
            return "No objectives."
        return "\n".join(
            f"OBJ-{o.seq} [{o.status}] {o.title} role={o.role_id or '-'}" for o in rows
        )

    def update_objective_status(self, seq: int, status: str, note: str = "") -> str:
        st = (status or "").strip().lower()
        if st not in OBJECTIVE_STATUSES:
            return f"Invalid status {st!r}; want one of {sorted(OBJECTIVE_STATUSES)}"
        pid, sched = self.job.project_id, self.plane.scheduler
        obj = next((o for o in self.store.get_objectives(pid) if o.seq == int(seq)), None)
        if obj is None:
            return f"Objective OBJ-{seq} not found."
        reason = (note or "").strip()
        own = obj.seq == self.job.objective_seq
        # "Release to scheduler" must create a job — status alone stalls the plan.
        if st == "in_progress":
            if own:
                sched.mark(pid, obj.seq, "in_progress")
                return f"OBJ-{obj.seq} → in_progress"
            job = sched.create_run(pid, obj)
            now = next((o for o in self.store.get_objectives(pid) if o.seq == obj.seq), obj)
            if job is None:
                return f"OBJ-{obj.seq} → {now.status}" + (f" ({now.note})" if now.note else "") + " — no job queued"
            return f"OBJ-{obj.seq} → {now.status}; queued job {job.id}"
        if st == "completed" and sched.active_jobs(pid, obj.seq):
            others = [j for j in sched.active_jobs(pid, obj.seq) if j.id != self.job.id]
            if not others:
                return (f"OBJ-{obj.seq} will complete when you finish your turn — no other jobs are active. "
                        "Give your final summary now; do not spawn more work to close it.")
            return f"OBJ-{obj.seq} remains {obj.status}; {len(others)} other tagged job(s) still active"
        sched.mark(pid, obj.seq, st, reason=reason)
        now = next((o for o in self.store.get_objectives(pid) if o.seq == obj.seq), obj)
        if st == "blocked":
            self.plane.governance.try_notify(
                pid, from_job_id=self.job.id, escalate=True,
                text=f"OBJ-{obj.seq} {obj.title} blocked: {reason or now.note or 'blocked'}")
            prompt = {"id": uuid.uuid4().hex[:12], "job_id": self.job.id, "kind": "blocked_objective",
                      "objective_seq": obj.seq, "blocks_job": False, "options": [], "answer": "", "at": _now(),
                      "question": f"OBJ-{obj.seq} {obj.title}: {reason or now.note or 'Objective blocked'}"}
            self.plane.raise_prompt(pid, prompt)
        elif st == "completed" and not own:
            queued = sched.enqueue_ready(pid)  # completing a peer objective advances the plan
            if queued:
                return (f"OBJ-{obj.seq} → {now.status}; queued {len(queued)} ready job(s): "
                        + ", ".join(j.id for j in queued[:8]))
        self.emit("status", f"OBJ-{obj.seq} → {st}", metadata={"event": "objective_status"})
        return f"OBJ-{obj.seq} → {now.status}"

    # -- operator -----------------------------------------------------------------

    def ask_operator(
        self, question: str, *, options: str = "", blocks_job: bool = True
    ) -> str:
        text = (question or "").strip()
        if not text:
            return "Error: question is required."
        opts = [o.strip() for o in (options or "").replace(";", ",").split(",") if o.strip()]
        prompt = {
            "id": uuid.uuid4().hex[:12],
            "job_id": self.job.id,
            "question": text,
            "options": opts,
            "blocks_job": bool(blocks_job),
            "kind": "agent_ask",
            "objective_seq": self.job.objective_seq,
            "at": _now(),
            "answer": "",
        }
        self.store.add_prompt(self.job.project_id, prompt)
        if blocks_job and self.job.objective_seq:
            self.plane.scheduler.mark(self.job.project_id, self.job.objective_seq, "blocked",
                                      reason=f"Waiting on operator: {text}")
        self.emit("log", f"Operator input needed: {text}", metadata={"event": "operator_prompt", "prompt": prompt})
        answer = self.plane.answer_prompt(self.job.project_id, prompt)
        if answer:
            if self.job.objective_seq:
                self.plane.scheduler.resume_blocked(self.job.project_id, self.job.objective_seq)
            return f"Operator answered: {answer}"
        return (
            f"Operator prompt {prompt['id']} created. Continue other work or check_inbox "
            "after the operator replies (plane.reply_to_prompt)."
        )

    def drain_operator_guidance(self, kinds: set[str] | None = None) -> list[str]:
        del kinds
        return self.store.pop_guidance(self.job.id)

    # -- findings / assets ---------------------------------------------------------

    def _finding_payload(self, fields: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": str(fields.get("title") or "").strip(),
            "severity": str(fields.get("severity") or "info"),
            "kind": str(fields.get("kind") or "observation"),
            "evidence": str(fields.get("evidence") or ""),
            "host": str(fields.get("host") or ""),
            "description": str(fields.get("description") or fields.get("summary") or ""),
            "asset_type": str(fields.get("asset_type") or ""),
            "evidence_path": str(fields.get("evidence_path") or ""),
            "remediation": str(fields.get("remediation") or ""),
            "service": str(fields.get("service") or ""),
            "port": fields.get("port"),
            "protocol": str(fields.get("protocol") or ""),
            "cve_id": str(fields.get("cve_id") or ""),
            "cwe_id": str(fields.get("cwe_id") or ""),
            "subjects": fields.get("subjects") if isinstance(fields.get("subjects"), list) else None,
            "metadata": fields.get("metadata") if isinstance(fields.get("metadata"), dict) else {},
        }

    def _record(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        return self.plane.findings_service.record(
            self.job.project_id, self._finding_payload(payload), job_id=self.job.id,
            role_id=self._role_id(), objective_seq=self.job.objective_seq)

    def record_finding(self, **fields: Any) -> str:
        if not str(fields.get("title") or "").strip():
            return "Error: title required."
        row = self._record(fields)
        if row is None:
            return "Finding not recorded (deduped or invalid)."
        msg = f"Recorded FIND-{row['seq']}: {row['title'][:80]}"
        self.emit("tool", msg, metadata=envelope(EVENT_RECORD_FINDING, {"seq": row["seq"]}))
        return msg

    def record_findings(self, findings_json: str) -> str:
        try:
            rows = json.loads(findings_json or "[]")
        except json.JSONDecodeError as exc:
            return f"Invalid JSON: {exc}"
        if not isinstance(rows, list):
            return "Expected a JSON list."
        n = 0
        for item in rows:
            if isinstance(item, dict) and str(item.get("title") or "").strip() and self._record(item):
                n += 1
        if n:
            self.emit("tool", f"Recorded {n} finding(s).", metadata=envelope(EVENT_RECORD_FINDING, {"count": n}))
        return f"Recorded {n} finding(s)."

    def list_findings(self, kind: str = "") -> str:
        rows = [f for f in self.store.get_findings(self.job.project_id)
                if not kind.strip() or f.get("kind") == kind.strip()]
        if not rows:
            return "No findings."
        return "\n".join(
            f"FIND-{f['seq']} [{f.get('severity')}/{f.get('kind')}] {f['title']}"
            + (f" host={f['host']}" if f.get("host") else "")
            + (f" port={f['port']}" if f.get("port") is not None else "")
            + (f" — {(f.get('evidence') or f.get('description') or '')[:120]}"
               if (f.get("evidence") or f.get("description")) else "")
            for f in reversed(rows[-500:])
        )

    def record_assets(self, assets_json: str, relations_json: str = "") -> str:
        """Producer API for the asset graph: assets + typed relations, with provenance."""
        def parse(raw: str, label: str):
            text = (raw or "").strip()
            if not text:
                return [], ""
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                return None, f"Invalid {label} JSON: {exc}"
            return (value, "") if isinstance(value, list) else (None, f"{label} must be a JSON list.")

        assets, err = parse(assets_json, "assets_json")
        if err:
            return err
        relations, err = parse(relations_json, "relations_json")
        if err:
            return err
        if not assets and not relations:
            return "Nothing to record: pass assets_json and/or relations_json."
        from orchestrator.engagement.asset_graph import AssetGraphEngine, Project as GraphProject

        role = self._role_id() or "agent"
        try:
            with self.store.lock(f"graph:{self.job.project_id}"):
                meta = self.store.get_project(self.job.project_id)
                report = AssetGraphEngine(
                    GraphProject(self.job.project_id, self.store, roe=meta.roe)
                ).record(assets, relations, source=role)
        except Exception as exc:  # noqa: BLE001
            logger.warning("record_assets failed: %s", exc, exc_info=True)
            return f"Error: could not update the asset graph ({exc})"
        msg = (f"Recorded {report['assets']} asset(s), {report['relations']} relation(s); "
               f"graph now {report['nodes']} nodes / {report['edges']} edges.")
        if report["skipped"]:
            msg += " Skipped: " + "; ".join(report["skipped"][:6])
        return msg

    # -- memory ---------------------------------------------------------------------

    def recall_memory(self, query: str) -> str:
        from orchestrator.memory import store

        if not (query or "").strip():
            return "Error: query is required."
        try:
            return store.recall_text(self.job.project_id, query, role_id=self._role_id())
        except Exception as exc:  # noqa: BLE001
            logger.warning("recall_memory failed: %s", exc, exc_info=True)
            return f"Error: memory recall failed ({exc})"

    def save_memory(
        self, name: str, kind: str, description: str, body: str = "", scope: str = "project"
    ) -> str:
        from orchestrator.memory import store

        scope_s = (scope or "project").strip().lower()
        if scope_s not in {"project", "platform"}:
            return "Error: scope must be 'project' or 'platform'."
        roe = self.project.roe
        try:
            entry, created = store.save(
                self.job.project_id if scope_s == "project" else None,
                name=name,
                kind=(kind or "").strip().lower(),
                description=description,
                body=body,
                role_id=self._role_id(),
                job_id=self.job.id,
                created_by=self._role_id() or "agent",
                scope_values=(roe.in_scope + roe.seed) if roe else (),
            )
        except ValueError as exc:
            return f"Error: {exc}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("save_memory failed: %s", exc, exc_info=True)
            return f"Error: could not save memory ({exc})"
        return f"{'Saved' if created else 'Updated'} {scope_s} memory [{entry.kind}] {entry.name}."

    def forget_memory(self, name: str, scope: str = "project") -> str:
        from orchestrator.memory import store

        scope_s = (scope or "project").strip().lower()
        if scope_s not in {"project", "platform"}:
            return "Error: scope must be 'project' or 'platform'."
        ok = store.forget(self.job.project_id if scope_s == "project" else None, name)
        return (
            f"Retired {scope_s} memory {store.slugify(name)}."
            if ok
            else f"No active {scope_s} memory named {name!r}."
        )

    # -- A2A --------------------------------------------------------------------------

    def other_agents_alive(self) -> bool:
        return any(
            j.id != self.job.id and j.status not in TERMINAL and j.profile != "governance"
            for j in self.store.list_jobs(self.job.project_id)
        )

    # -- A2A addressing / rendering (Peon: a2a.resolve_recipient / team_listing / render_messages) ----

    @staticmethod
    def _short(job_id: str) -> str:
        return str(job_id or "").replace("-", "")[:8]

    def _label(self, job: JobState | None, fallback: str = "system") -> str:
        """``role (id8)`` for an agent; the fallback name for a non-agent sender."""
        if job is None:
            return fallback
        role = ((job.role_ids[0] if job.role_ids else "") or job.title or "agent").strip()
        return f"{role} ({self._short(job.id)})"

    def _resolve_recipient(self, raw: str) -> tuple[JobState | None, str]:
        """The ONE agent a message is addressed to: its id (full, or a unique prefix of >= 8 chars).

        A role id or objective names a *group*, so it is never delivered — the reason lists the matching ids.
        """
        text = (raw or "").strip().lower()
        if not text:
            return None, "recipient is required: the agent id from list_team"
        agents = self.store.list_jobs(self.job.project_id)
        compact = text.replace("-", "")
        if re.fullmatch(r"[0-9a-f]{8,32}", compact):
            hits = [j for j in agents if j.id.replace("-", "").startswith(compact)]
            if not hits:
                return None, f"no agent with id {text} on this project"
            if len(hits) > 1:
                listing = "; ".join(f"{j.id} ({self._label(j)})" for j in hits[:6])
                return None, f"id prefix {text!r} matches several agents — use more characters: {listing}"
            job = hits[0]
            if job.status in {"failed", "cancelled"}:
                return None, (f"agent {job.title!r} {job.status} and cannot be resumed by messaging it; "
                              "spawn new work instead")
            return job, ""   # a completed agent is resumed from its checkpoint by the message
        token = text.removeprefix("obj-").removeprefix("obj")
        live = [j for j in agents if j.status not in TERMINAL and j.id != self.job.id]
        matches = [j for j in live if text in [r.lower() for r in j.role_ids]
                   or (token.isdigit() and j.objective_seq == int(token))]
        hint = (" Matching agents: " + "; ".join(f"{j.id} ({self._label(j)})" for j in matches[:6])) if matches else ""
        return None, f"{raw!r} is not an agent id — messages go to one agent, addressed by its id (see list_team).{hint}"

    def send_message(
        self, to: str, content: str, *, kind: str = "inform", reply_to: str = ""
    ) -> str:
        ids = [t.strip() for t in (to or "").split(",") if t.strip()]
        if not ids:
            return "Error: to is required (one or more agent ids, comma-separated)."
        if not (content or "").strip():
            return "Error: content is required."
        hub = self.plane.hub
        if len(ids) > hub.max_recipients:
            return f"Error: {len(ids)} recipients exceeds the limit of {hub.max_recipients} per send."
        # Resolve every id before sending anything — a partial multi-send would leave the caller unsure who got it.
        targets: list[JobState] = []
        for raw in ids:
            target, why = self._resolve_recipient(raw)
            if target is None:
                return f"Error: {raw!r} — {why}. Use list_team to see who you can message."
            if target.id == self.job.id:
                return "Error: that is you."
            targets.append(target)
        reply_id = (reply_to or "").strip()
        if reply_id and not any(m.id == reply_id and m.recipient_job_id == self.job.id
                                for m in self.plane.hub.inbox(self.job.id)):
            return (f"Error: {reply_to!r} is not a message you received — reply_to must be a message_id from "
                    "your own inbox.")
        sent = []
        for target in targets:
            try:
                msg = hub.send(
                    sender_job_id=self.job.id,
                    recipient_job_id=target.id,
                    kind=kind if kind in {"inform", "request", "reply"} else "inform",
                    content=(content or "").strip()[:8000],
                    reply_to=reply_id or None,
                    wake=self.plane.wake,
                )
            except ValueError as exc:
                sent.append(f"{self._label(target)}: Error — {exc}")
                continue
            now = self.store.get_job(target.id) or target
            state = "woken and queued" if now.status == "pending" else f"delivered ({now.status})"
            sent.append(f"{self._label(target)} [{state}], message id {msg.id}")
        return (
            ("Sent:\n" if any("Error —" not in line for line in sent) else "Not sent:\n")
            + "\n".join(f"- {line}" for line in sent)
            + "\n\nReplies arrive in your inbox (check_inbox); use message_status to check whether a specific "
            "send was read."
        )

    def message_status(self, limit: int = 10) -> str:
        """Delivery/read state of this agent's own recent sends (most recent first)."""
        by_id = {j.id: j for j in self.store.list_jobs(self.job.project_id)}
        inbox = self.plane.hub.inbox(self.job.id)
        sent = sorted((m for m in inbox if m.sender_job_id == self.job.id), key=lambda m: m.created_at, reverse=True)
        if not sent:
            return "You have not sent any messages yet."
        lines = []
        for m in sent[: max(1, min(50, int(limit or 10)))]:
            reply = next((r for r in sorted(inbox, key=lambda r: r.created_at) if r.reply_to == m.id), None)
            flags = ["read" if m.read_at else "unread"] + (["replied"] if reply else [])
            line = f"{m.id} → {self._label(by_id.get(m.recipient_job_id), fallback=m.recipient_job_id)} [{', '.join(flags)}]"
            if reply:
                line += f"\n  reply: {reply.content[:300]}"
            lines.append(line)
        return "\n".join(lines)

    def render_messages(self, rows: list) -> str:
        """Prompt block for an agent picking up (or resuming) work with unread messages."""
        rows = rows[:12]
        if not rows:
            return ""
        by_id = {j.id: j for j in self.store.list_jobs(self.job.project_id)}
        blocks = []
        for m in rows:
            label = {"handoff": "HANDOFF", "request": "REQUEST", "reply": "REPLY", "operator": "OPERATOR"}.get(m.kind, "MESSAGE")
            sender = by_id.get(m.sender_job_id)
            who = self._label(sender) if sender else (m.sender_job_id or "system")
            blocks.append(f"[{label} from {who}] (message_id={m.id})\n{m.content}")
        return (
            "## Messages from other agents\n"
            "You were woken (or resumed) with these messages. Act on them under Rules of Engagement; reply with "
            "`send_message` if the sender needs an answer.\n\n" + "\n\n".join(blocks)
        )

    def check_inbox(self) -> str:
        parts: list[str] = []
        guidance = self.drain_operator_guidance()
        if guidance:
            parts.append("## Operator guidance\n" + "\n\n".join(guidance))
        rows = [m for m in self.plane.hub.inbox(self.job.id, unread_only=True) if m.recipient_job_id == self.job.id]
        if rows:
            parts.append(self.render_messages(rows))
            for m in rows:
                self.plane.hub.mark_read(m.id)
        return "\n\n".join(parts) if parts else "Inbox empty."

    def go_idle(self, reason: str = "") -> str:
        if not self.other_agents_alive():
            return (
                "Not idling: no other agent is alive to message you. "
                "Finish your work and report your result instead."
            )
        self.idle_requested = True
        self.idle_note = (reason or "waiting for another agent")[:255]
        return (
            "Understood — you will be parked as IDLE when you finish this turn and woken, "
            "with your context, when another agent or the operator messages you. End your "
            "turn now with a one-paragraph summary of what you completed and exactly what "
            "you are waiting for."
        )

    _A2A_STATE = {"pending": "submitted", "running": "working", "idle": "input-required", "paused": "input-required",
                  "completed": "completed", "failed": "failed", "cancelled": "canceled"}

    def list_team(self) -> str:
        """One line per live agent: who to message and what state it is in."""
        jobs = [j for j in self.store.list_jobs(self.job.project_id) if j.status not in TERMINAL]
        jobs.sort(key=lambda j: (j.objective_seq, j.created_at))
        lines = []
        for j in jobs[:80]:
            me = " (you)" if j.id == self.job.id else ""
            seq = f"OBJ-{j.objective_seq}" if j.objective_seq else "-"
            parent = f" parent={self._short(j.parent_id)}" if j.parent_id else ""
            wait = f" — waiting: {j.idle_note}" if j.status == "idle" and j.idle_note else ""
            lines.append(f"id={j.id} {seq} role={','.join(j.role_ids) or '-'}{parent} "
                         f"state={self._A2A_STATE.get(j.status, 'unknown')} [{j.status}]{me}{wait}")
        header = "Address a message to ONE agent by its id (full id, or its first 8 characters):"
        return header + "\n" + "\n".join(lines) if lines else "No live agents on this project."

    # -- workspace artifacts ------------------------------------------------------------

    def write_workspace_artifact(self, path: str, content: str) -> str:
        rel = (path or "").strip().lstrip("/")
        parts = Path(rel).parts
        if not rel or ".." in parts or not parts or parts[0] != "workspace":
            return "Error: path must be relative and under workspace/."
        text = content if isinstance(content, str) else str(content or "")
        if len(text.encode("utf-8")) > 2_000_000:
            return "Error: content exceeds 2 MB."
        target = safe_join(self.workspace.resolve(), rel)
        if target is None:
            return "Error: path escapes workspace."
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(target)
        except OSError as exc:
            return f"Error writing {rel}: {exc}"
        self.emit("tool", f"Wrote {rel}", metadata=envelope(EVENT_WRITE_ARTIFACT, {"path": rel}))
        return f"Wrote {rel} ({len(text)} chars)."

    def list_workspace_artifacts(self) -> str:
        root = self.workspace
        rows: list[str] = []
        for sub in ("workspace", "findings"):
            base = root / sub
            if not base.is_dir():
                continue
            for p in sorted(base.rglob("*")):
                if p.is_file() and not p.name.startswith("."):
                    rows.append(f"{p.relative_to(root).as_posix()} ({p.stat().st_size} bytes)")
        return "\n".join(rows[:300]) or "(no workspace artifacts yet)"

    def read_workspace_artifact(self, path: str, max_chars: int = 500_000) -> str:
        rel = (path or "").strip().lstrip("/")
        if not rel or ".." in Path(rel).parts:
            return "Error: provide a relative path under workspace/ or findings/."
        root = self.workspace.resolve()
        target = safe_join(root, rel)
        if target is None or not target.is_file():
            return f"Error: artifact not found: {rel}"
        top = target.relative_to(root).parts[0]
        if top not in {"workspace", "findings"}:
            return "Error: only workspace/ and findings/ artifacts are readable."
        self.emit("tool", f"read {rel}", metadata=envelope(EVENT_READ_ARTIFACT, {"path": rel}))
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"Error reading {rel}: {exc}"
        limit = max(1_000, min(int(max_chars or 500_000), 5_000_000))
        if len(text) > limit:
            return text[:limit].rstrip() + f"\n\n…(truncated at {limit} chars of {len(text)}; file={rel})"
        return text

    def write_report_note(self, section: str, body: str) -> str:
        heading = (section or "note").strip()[:120] or "note"
        text = (body or "").strip()
        if not text:
            return "Error: report note body required."
        report_dir = self.workspace / "findings"
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / "report.md"
        with self.plane.lock, _file_lock(report_dir / ".report.lock"):
            existing = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
            if not existing.strip():
                existing = f"# Report — {self.project.title or self.job.project_id}\n"
            lines = existing.split("\n")
            want = f"## {heading}".lower()
            start = next((i for i, ln in enumerate(lines) if ln.strip().lower() == want), None)
            if start is None:
                updated = existing.rstrip() + f"\n\n## {heading}\n\n{text}\n"
            else:
                end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
                updated = "\n".join(lines[:start] + [lines[start], "", text, ""] + lines[end:]).rstrip() + "\n"
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(updated, encoding="utf-8")
            tmp.replace(path)
        self.emit("status", f"Report note appended: {heading}",
                  metadata=envelope(EVENT_REPORT_NOTE, {"section": heading}))
        return f"Appended report section '{heading}' to findings/report.md"

    # -- RoE ----------------------------------------------------------------------------
    # ``Roe is None`` = unrestricted (a library project without rules); once a Roe exists these follow Peon exactly.

    def _roe(self):
        return self.project.roe

    def roe_summary(self) -> str:
        roe = self._roe()
        return roe.summary() if roe else "No Rules of Engagement set: scope is unrestricted."

    def roe_probe_block_reason(self) -> str:
        """Empty when in_scope is usable for networked probes; else the deny reason."""
        from orchestrator.engagement.target_shapes import coerce_targets

        roe = self._roe()
        if roe is None:
            return ""
        if not any((t.get("value") or "").strip() for t in coerce_targets(roe.in_scope)):
            return "in_scope is empty — promote targets before active probing"
        return ""

    def assert_in_scope(self, target: str) -> str:
        """Empty string = allowed; otherwise a denial reason."""
        from orchestrator.engagement.target_shapes import (
            coerce_targets, hostname_of, is_passive_reference_host, keep_stop_hosts,
            target_authorized_by_scope)

        value = (target or "").strip()
        if not value:
            return "empty target"
        roe = self._roe()
        if roe is None:
            return ""

        def vals(raw) -> list[str]:
            return [(t.get("value") or "").strip().lower() for t in coerce_targets(raw) if t.get("value")]

        with keep_stop_hosts():
            exclusions = vals(roe.exclusions)
        scope_vals, seed_vals = vals(roe.in_scope), vals(roe.seed)
        needle = value.lower()
        if target_authorized_by_scope(needle, exclusions):
            return f"{value!r} is excluded by RoE"
        host = hostname_of(value)
        if is_passive_reference_host(host):  # a research API an MCP server queries: provenance, not a target
            if host and target_authorized_by_scope(host, exclusions):
                return f"{value!r} is excluded by RoE"
            if scope_vals or seed_vals:
                return ""
            return "no engagement subject — cannot use passive reference hosts"
        if not scope_vals:
            return self.roe_probe_block_reason() or "in_scope is empty — promote targets before active probing"
        if target_authorized_by_scope(needle, scope_vals):
            return ""
        return f"{value!r} is not in authorized RoE scope"

    def duplicate_scan_reason(self, command: str) -> str:
        """Non-empty when this job already ran the same catalog CLI against the same host."""
        from orchestrator.utils.commands import catalog_cli_in_command
        from orchestrator.utils.stream_events import (
            EVENT_RUN_CLI, event_of, hosts_in_command, is_event, payload_cli, payload_command, payload_hosts)

        cmd = (command or "").strip()
        cli = catalog_cli_in_command(cmd)
        if not cli:
            return ""
        hosts = set(hosts_in_command(cmd))
        if not hosts:
            return ""
        me = self.store.get_job(self.job.id, events=True)
        prior = [e for e in (me.events if me else []) if e.get("message_type") == "tool"][-40:]
        for ev in reversed(prior):
            meta = ev.get("metadata") if isinstance(ev.get("metadata"), dict) else {}
            if event_of(meta) and not is_event(meta, EVENT_RUN_CLI):
                continue
            prev_cmd = payload_command(meta)
            if not prev_cmd:
                continue
            if (payload_cli(meta) or catalog_cli_in_command(prev_cmd)) != cli:
                continue
            if hosts & set(payload_hosts(meta) or hosts_in_command(prev_cmd)):
                return (f"similar {cli} already ran this job for {', '.join(sorted(hosts))}. Reuse the existing "
                        "workspace scan output — do not re-run the scan.")
        return ""

    def assert_command_allowed(self, command: str) -> str:
        """Block active probe commands when RoE scope is empty or a target is denied.

        Local inspection (``ls``/``cat``/``python3`` on workspace files) is not a probe — host checks apply only
        when a catalog/role CLI is invoked.
        """
        from orchestrator.utils.commands import catalog_cli_in_command
        from orchestrator.utils.stream_events import hosts_in_command

        if (why := self.interrupted()):
            return f"{why} — stop running commands and end your turn"
        cmd = (command or "").strip()
        if not cmd:
            return "empty command"
        if not catalog_cli_in_command(cmd):
            return ""
        roe = self._roe()
        if roe is None:
            return ""
        found = set(hosts_in_command(cmd))
        if not found:
            return ""
        if not self.roe_probe_block_reason() == "":
            return "in_scope is empty — cannot run networked probe commands"
        for hit in found:
            if (reason := self.assert_in_scope(hit)):
                return reason
        return ""


# ── executors ───────────────────────────────────────────────────────────────────


class ThreadExecutor:
    """Run jobs on an in-process thread pool (default; one process)."""

    def __init__(self, max_workers: int = 8) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="orch-job")
        self.plane: "LocalControlPlane | None" = None

    def dispatch(self, job_id: str, resume: bool = False, steer: str = "", delay_ms: int = 0) -> None:
        assert self.plane is not None, "executor is not attached to a control plane"
        if delay_ms > 0:
            timer = threading.Timer(delay_ms / 1000.0, self._pool.submit, args=(self.plane.execute, job_id, resume, steer))
            timer.daemon = True
            timer.start()
        else:
            self._pool.submit(self.plane.execute, job_id, resume, steer)

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=not wait)


# ── control plane ───────────────────────────────────────────────────────────────


class LocalControlPlane:
    """Registers jobs, dispatches them to an executor, and owns the shared state.

    ``store`` holds all run state (``MemoryStateStore`` by default, ``RedisStateStore`` to share it
    across processes); ``executor`` decides where a job runs (``ThreadExecutor`` in-process, or the
    Dramatiq executor from ``orchestrator.queue`` on workers). ``execute`` is what runs *in* the
    worker: it binds the sandbox and calls ``run_agent`` for one job.
    """

    def __init__(
        self,
        *,
        on_event: EventHook | None = None,
        hub: A2AHub | None = None,
        run_log: JsonRunLog | None = None,
        operator: Callable[[dict[str, Any]], str] | None = None,
        agent_config: AgentRunConfig | None = None,
        max_workers: int = 8,
        sandbox: bool = True,
        socket_dir: str | Path = "",
        store: StateStore | None = None,
        executor: Any = None,
    ) -> None:
        cfg = get_config()
        self.on_event = on_event
        self.hub = hub or A2AHub(
            max_recipients=max(1, settings.get_int("A2A_MAX_RECIPIENTS", 5)),
            max_messages_per_pair=max(1, settings.get_int("A2A_MAX_MESSAGES_PER_PAIR", 40)),
        )
        self.run_log = run_log or JsonRunLog()
        self.operator = operator
        self.sandbox = sandbox
        self.socket_dir = str(socket_dir or "")
        self.store: StateStore = store or MemoryStateStore()
        self.agent_config = agent_config or AgentRunConfig(
            max_failure_replans=cfg.agent_max_failure_replans,
            max_iterations=cfg.agent_max_iterations,
            max_subagents=cfg.agent_max_subagents,
            max_subagent_depth=cfg.agent_max_subagent_depth,
            runtime_enabled=cfg.agent_runtime_enabled,
            crew_reasoning_effort=cfg.crew_reasoning_effort,
            crew_reasoning_max_attempts=cfg.crew_reasoning_max_attempts,
        )
        self.lock = threading.RLock()
        self._provisioned: set[str] = set()
        self.executor = executor or ThreadExecutor(max_workers)
        self.stream_socket = ""
        self._stream_server: Any = None
        self.scheduler = ObjectiveScheduler(self)
        self.schedules = ScheduleManager(self)
        self.governance = GovernanceManager(self)
        from orchestrator.host.operator import OperatorControl

        self.operator_control = OperatorControl(self)
        from orchestrator.engagement.findings import FindingService

        self.findings_service = FindingService(self)
        if hasattr(self.executor, "plane") and getattr(self.executor, "plane") is None:
            self.executor.plane = self

    # -- state ---------------------------------------------------------------------

    def project(self, project_id: str, *, title: str = "", roe: Roe | None = None) -> ProjectMeta:
        pid = str(project_id or "").strip() or "default"
        if title or roe is not None:
            return self.store.set_project(pid, title=title, roe=roe)
        return self.store.get_project(pid)

    def workspace(self, project_id: str) -> Path:
        ws = Path(get_config().workspaces_dir) / safe_workspace_key(project_id)
        ensure_workspace_layout(ws)
        return ws

    def set_objectives(self, project_id: str, objectives: list[Objective]) -> None:
        self.store.set_objectives(project_id, objectives)

    def objectives(self, project_id: str) -> list[Objective]:
        return self.store.get_objectives(project_id)

    def findings(self, project_id: str) -> list[dict[str, Any]]:
        return self.store.get_findings(project_id)

    def prompts(self, project_id: str) -> list[dict[str, Any]]:
        return self.store.get_prompts(project_id)

    def save_findings(self, project_id: str) -> None:
        try:
            path = self.workspace(project_id) / "findings" / "findings.json"
            path.write_text(json.dumps(self.store.get_findings(project_id), indent=2), encoding="utf-8")
        except OSError as exc:
            logger.debug("saving findings failed: %s", exc)

    def start_stream_server(self, path: str = "") -> str:
        """Listen for live events pushed by skills in the sandbox (``skills/helpers`` ``stream()``).

        Path: ``path`` → ``ORCHESTRATOR_STREAM_SOCKET`` → ``/tmp/orchestrator/stream.sock`` when
        ``SANDBOX_SOCKETS_VOLUME`` is set (containerised workers) → ``/tmp/orchestrator-<uid>/stream.sock``.
        Its directory is mounted into the project sandbox at the same path. Idempotent.
        """
        import os

        from orchestrator.runtime.stream_server import StreamSocketServer

        if self._stream_server is not None:
            return self.stream_socket
        sock = (path or settings.get_str("ORCHESTRATOR_STREAM_SOCKET")
                or ("/tmp/orchestrator/stream.sock" if settings.get_str("SANDBOX_SOCKETS_VOLUME")
                    else f"/tmp/orchestrator-{os.getuid()}/stream.sock"))
        self._stream_server = StreamSocketServer(sock, self._on_stream)
        self._stream_server.start()
        self.stream_socket = sock
        self.socket_dir = self.socket_dir or os.path.dirname(sock)
        return sock

    def stop_stream_server(self) -> None:
        if self._stream_server is not None:
            self._stream_server.stop()
            self._stream_server, self.stream_socket = None, ""

    def _on_stream(self, payload: dict[str, Any]) -> None:
        """A message from a skill script: append it to the job's event stream (and notify the host hook)."""
        job_id = str(payload.get("job_id") or payload.get("task_id") or "")
        job = self.store.get_job(job_id) if job_id else None
        if job is None:
            return
        event = {
            "job_id": job.id, "project_id": job.project_id,
            "message_type": str(payload.get("message_type") or "log"),
            "content": str(payload.get("content") or ""),
            "metadata": dict(payload.get("metadata") or {}), "at": _now(),
        }
        self.store.add_event(job.id, event)
        self.dispatch(event)

    def dispatch(self, event: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:  # noqa: BLE001 — a host hook must not break a run
                logger.exception("on_event hook failed")

    def answer_prompt(self, project_id: str, prompt: dict[str, Any]) -> str:
        if self.operator is None:
            return ""
        try:
            answer = str(self.operator(prompt) or "").strip()
        except Exception:  # noqa: BLE001
            logger.exception("operator hook failed")
            return ""
        self.store.set_prompt_answer(project_id, prompt["id"], answer)
        return answer

    def raise_prompt(self, project_id: str, prompt: dict[str, Any]) -> None:
        """Store an operator prompt; a host hook may answer it synchronously (then it is applied at once)."""
        self.store.add_prompt(project_id, prompt)
        answer = self.answer_prompt(project_id, prompt)
        if answer:
            self.reply_to_prompt(project_id, prompt["id"], answer)

    def reply_to_prompt(self, project_id: str, prompt_id: str, answer: str) -> bool:
        """Deliver an operator reply to a prompt (Peon: ``hitl.resolve_operator_prompt``).

        The reply is durable knowledge (feedback memory + transcript + platform lesson), resumes the prompt's
        blocked objective, and steers the asking job — or re-queues it when it already finished.
        """
        text = (answer or "").strip()
        if not text:
            raise ValueError("Reply is required")
        prompt = self.store.set_prompt_answer(project_id, prompt_id, text)
        if prompt is None:
            return False
        stub = text.upper().startswith(("APPROVED BY OPERATOR", "DENIED BY OPERATOR"))
        if not stub:  # a typed answer is knowledge (scope, credentials policy…); bare approve/deny stubs are not
            self._remember_feedback(
                project_id, f"Question from an agent: {prompt.get('question', '')}\nOperator answer: {text}",
                source="reply to an agent question")
        if self.scheduler.resolve_prompt(project_id, prompt, text):
            self._record_operator_guidance(project_id, prompt, text)
            return True  # failure-exhausted prompts have their own approve/guidance/deny handling
        note = (f"OPERATOR REPLY (human-in-the-loop):\nKind: {prompt.get('kind', 'agent_ask')}\n"
                f"Question: {prompt.get('question', '')}\nAnswer: {text}\n\n"
                "Honor this under Rules of Engagement and continue the objective.")
        job = self.store.get_job(prompt.get("job_id", ""))
        if job is not None and job.objective_seq:
            self.scheduler.resume_blocked(project_id, job.objective_seq, reason="Operator replied")
        if job is not None:
            self.store.add_guidance(job.id, note)
            if job.status in TERMINAL and job.status != "cancelled":
                self.requeue(job.id)   # a finished job picks the reply up from its checkpoint
            else:
                self.wake(job.id)
        self._record_operator_guidance(project_id, prompt, text, job=job)
        return True

    def _record_operator_guidance(self, project_id: str, prompt: dict[str, Any], text: str,
                                  job: JobState | None = None) -> None:
        """Durable learning from an operator reply: a structured transcript record + a platform memory note."""
        job = job or self.store.get_job(prompt.get("job_id", ""))
        try:
            rec = record_from_event(
                category="method",
                summary=f"Operator guidance ({prompt.get('kind', '')}): {text[:400]}",
                project_id=project_id, objective_id=str(job.objective_seq if job else ""),
                job_id=job.id if job else "", role_id=(job.role_ids[0] if job and job.role_ids else ""),
                event="operator_guidance",
                payload={"prompt_id": prompt.get("id", ""), "prompt_kind": prompt.get("kind", ""),
                         "question": str(prompt.get("question", ""))[:1000], "reply": text[:2000]})
            if job is not None:
                LocalAgentBridge(self, job).append_transcript(rec)
        except Exception as exc:  # noqa: BLE001
            logger.warning("operator reply transcript failed: %s", exc)
        try:
            from orchestrator.crew.memory import MemoryService

            MemoryService.remember_peon(
                f"Operator guidance on {prompt.get('kind', '')}: Q={str(prompt.get('question', ''))[:240]} A={text[:400]}",
                category="method", event="operator_guidance", project_id=project_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("remember_peon failed: %s", exc)

    def steer(self, job_id: str, text: str, *, remember: bool = True) -> bool:
        """Operator guidance for a live job (drained before its next turn); wakes it if idle."""
        if self.store.get_job(job_id) is None or not (text or "").strip():
            return False
        self.store.add_guidance(
            job_id, "OPERATOR INSTRUCTION — revise your approach and continue:\n" + text.strip()
        )
        job = self.store.get_job(job_id)
        if job is not None and remember:
            self._remember_feedback(job.project_id, text, source="operator steer")
        self.wake(job_id)
        return True

    def _remember_feedback(self, project_id: str, text: str, *, source: str) -> None:
        """Durable operator guidance → project memory (never blocks the caller)."""
        try:
            from orchestrator.memory import store as memory

            memory.record_feedback(project_id, text, source=source)
        except Exception:  # noqa: BLE001
            logger.debug("feedback capture failed", exc_info=True)

    def find(self, job_id: str, *, events: bool = False) -> JobState | None:
        return self.store.get_job(job_id, events=events)

    def jobs(self, project_id: str) -> list[JobState]:
        return self.store.list_jobs(project_id)

    # -- running ---------------------------------------------------------------------

    def submit(
        self,
        *,
        project_id: str,
        title: str = "",
        description: str = "",
        role_ids: list[str] | None = None,
        skill_ids: list[str] | None = None,
        parent_id: str = "",
        objective_seq: int = 0,
        depth: int = 1,
        extras: dict[str, Any] | None = None,
        dispatch: bool = True,
    ) -> JobState:
        """Register a job and hand it to the executor; returns immediately (``dispatch=False``: register only)."""
        job = JobState(
            id=uuid.uuid4().hex[:12],
            project_id=str(project_id or "default"),
            title=title or "job",
            description=description,
            role_ids=list(role_ids or []),
            skill_ids=list(skill_ids or []),
            parent_id=parent_id,
            objective_seq=objective_seq,
            depth=depth,
            extras=dict(extras or {}),
        )
        self.store.set_project(job.project_id)
        self.store.put_job(job)
        if dispatch:
            self.executor.dispatch(job.id, False, "")
        return job

    def run(self, **kwargs: Any) -> JobState:
        """Submit and block until the job reaches a terminal state."""
        job = self.submit(**kwargs)
        return self.join(job)

    def join(self, job: "JobState | str", timeout: float | None = None) -> JobState:
        """Block until the job is terminal (idle jobs with no live peer are woken). Returns it with events."""
        job_id = job if isinstance(job, str) else job.id
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            cur = self.store.get_job(job_id)
            if cur is None:
                raise KeyError(f"unknown job {job_id}")
            if cur.status in TERMINAL:
                return self.store.get_job(job_id, events=True) or cur
            if cur.status == "idle" and cur.idle_note != DEP_NOTE and not self.other_alive(cur):
                self.wake(job_id)
            if deadline is not None and time.monotonic() > deadline:
                return self.store.get_job(job_id, events=True) or cur
            time.sleep(0.1)

    def other_alive(self, job: JobState) -> bool:
        """Is some *other* agent alive that could still message this one (the resident governor does not count)?"""
        return any(
            j.id != job.id and j.status not in TERMINAL and j.profile != "governance"
            for j in self.store.list_jobs(job.project_id)
        )

    def wait_all(self, project_id: str, timeout: float | None = None) -> None:
        """Block until no job of the project is pending/running (all-idle jobs are woken)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            active = [j for j in self.store.list_jobs(project_id)
                      if j.status not in TERMINAL and j.profile != "governance"]
            if not active:
                return
            if not [j for j in active if j.status in {"pending", "running"}]:
                wakeable = [j for j in active if j.status == "idle" and j.idle_note != DEP_NOTE]
                if not wakeable:
                    return  # only dependency standbys are left: an upstream objective is blocked
                for j in wakeable:  # everything idle: nobody can wake anyone — resume them
                    self.wake(j.id)
            if deadline is not None and time.monotonic() > deadline:
                return
            time.sleep(0.2)

    def wake(self, job_id: str, *, force: bool = False) -> None:
        """Resume an idle job — or a *completed* one — because a message or guidance reached it.

        ``completed`` means "had nothing left to do"; a new message is a reason to continue, so the job is
        re-queued from its checkpoint (and its objective reopened). Failed/cancelled jobs stay stopped.
        """
        cur = self.store.get_job(job_id)
        if cur is not None and cur.status == "idle" and cur.idle_note == DEP_NOTE and not force:
            return  # a dependency standby is woken by the scheduler once its dependencies are met
        if self.store.claim_job(job_id, {"idle"}, "pending"):
            self.store.patch_job(job_id, control="")
            self.executor.dispatch(job_id, True, "")
        elif self.store.claim_job(job_id, {"completed"}, "pending"):
            self.store.patch_job(job_id, control="")
            job = self.store.get_job(job_id)
            if job is not None:
                self.scheduler.reopen_for_job(job)
            self.executor.dispatch(job_id, True, "")

    # -- operator job control (Peon: pause / resume / kill) ----------------------------------

    def pause(self, job_id: str) -> bool:
        """Pause a job. Pending/idle jobs pause immediately; a running job is asked to stop (cooperative:
        its next command/spawn is refused and the turn ends as ``paused`` with a checkpoint)."""
        if self.store.claim_job(job_id, {"pending", "idle"}, "paused"):
            return True
        job = self.store.get_job(job_id)
        if job is not None and job.status == "running":
            self.store.patch_job(job_id, control="pause")
            now = self.store.get_job(job_id)
            if now is None or now.status != "running":   # it finished meanwhile: the request would haunt a rerun
                self.store.patch_job(job_id, control="")
                return False
            return True
        return False

    def resume(self, job_id: str) -> bool:
        """Resume a paused job from its checkpoint."""
        self.store.patch_job(job_id, control="")
        cur = self.store.get_job(job_id)
        if cur is not None and cur.status == "paused" and cur.idle_note == DEP_NOTE:
            return self.store.claim_job(job_id, {"paused"}, "idle")   # still a standby: the scheduler starts it
        if self.store.claim_job(job_id, {"paused"}, "pending"):
            self.executor.dispatch(job_id, True, "")
            return True
        return False

    def kill(self, job_id: str, *, cascade: bool = True) -> bool:
        """Operator kill (Peon: ``apply_directive(job, "kill")``): cancel the job (and its live children) and
        withdraw its objective — or, for a job with none, the open objectives of its roles."""
        job = self.store.get_job(job_id)
        if job is None or job.status in TERMINAL:
            return False
        if not self.cancel(job_id):
            return False
        if cascade:
            for child in self.store.list_jobs(job.project_id):
                if child.parent_id == job.id and child.status not in TERMINAL:
                    self.kill(child.id)
        if job.objective_seq:
            self.scheduler.mark(job.project_id, job.objective_seq, "cancelled", reason="Cancelled by operator")
        else:
            for o in self.store.get_objectives(job.project_id):
                rids = {r.strip() for r in o.role_id.replace(";", ",").split(",") if r.strip()}
                if rids & set(job.role_ids) and o.status in {"pending", "in_progress", "blocked"}:
                    self.store.patch_objective(job.project_id, o.seq, status="cancelled", note="Cancelled by operator")
        return True

    def requeue(self, job_id: str) -> JobState | None:
        """Reset a job to ``pending`` and run it again (Peon: ``lifecycle.queue_job``): objective reopened,
        error/result cleared. A running or already-pending job is left alone."""
        job = self.store.get_job(job_id)
        if job is None or job.status in {"running", "pending"}:
            return job
        self.scheduler.reopen_for_job(job)
        self.store.patch_job(job_id, status="pending", error="", result="", control="", idle_note="")
        self.executor.dispatch(job_id, False, "")
        return self.store.get_job(job_id)

    def pause_project(self, project_id: str) -> int:
        if self.store.get_project(project_id).status != "stopped":   # a stopped project stays stopped
            self.store.set_project(project_id, status="paused")  # no new work is released, no schedule fires
        return sum(1 for j in self.store.list_jobs(project_id) if j.status not in TERMINAL and self.pause(j.id))

    def resume_project(self, project_id: str) -> int:
        if self.store.get_project(project_id).status == "stopped":
            return 0
        self.store.set_project(project_id, status="active")
        n = 0
        for j in self.store.list_jobs(project_id):
            if j.status == "paused" and self.resume(j.id):
                n += 1
            elif j.status == "running" and j.control == "pause":
                self.store.patch_job(j.id, control="")  # pause not honored yet: withdraw the request
                n += 1
        return n

    def cancel(self, job_id: str) -> bool:
        """Cancel a job (internal cascade / orphan cleanup). A running turn winds down: its commands are refused."""
        prev = self.store.get_job(job_id)
        if prev is not None and prev.status == "running":
            self.store.patch_job(job_id, control="kill", extras={**prev.extras, "terminal_pending": True})
        if self.store.claim_job(job_id, {"pending", "running", "idle", "paused"}, "cancelled"):
            self.store.patch_job(job_id, error="Cancelled", idle_note="")
            job = self.store.get_job(job_id)
            if job is not None and (prev is None or prev.status != "running"):
                self._after_terminal(job)  # a running turn is finished by its own worker
            return True
        if prev is not None and prev.status == "running":   # lost the race to a job that finished first
            cur = self.store.get_job(job_id)
            if cur is not None and cur.status != "cancelled":
                self.store.patch_job(job_id, control="", extras={k: v for k, v in cur.extras.items() if k != "terminal_pending"})
        return False

    def _bind_runtime(self, job: JobState, ws: Path, bridge: "LocalAgentBridge | None" = None):
        cfg = get_config()
        env = {
            "ORCHESTRATOR_JOB_ID": job.id,
            "ORCHESTRATOR_PROJECT_ID": job.project_id,
            "ORCHESTRATOR_WORKSPACE": str(ws),
            "PROJECT_WORKSPACES_DIR": str(cfg.workspaces_dir),
            "SKILLS_DIR": str(cfg.skills_dir),
            "TOOLS_CATALOG_DIR": str(cfg.tools_catalog_dir),
        }
        if self.stream_socket or settings.get_str("ORCHESTRATOR_STREAM_SOCKET"):
            env["ORCHESTRATOR_STREAM_SOCKET"] = self.stream_socket or settings.get_str("ORCHESTRATOR_STREAM_SOCKET")
        if job.role_ids:
            env["ORCHESTRATOR_ROLE_ID"] = job.role_ids[0]
        if job.description or job.title:
            env["ORCHESTRATOR_JOB_BRIEF"] = (job.description or job.title).strip()[:4000]
        roe = self.store.get_project(job.project_id).roe
        if roe is not None:
            env["ORCHESTRATOR_IN_SCOPE"] = json.dumps([{"value": v} for v in roe.in_scope])
            env["ORCHESTRATOR_EXCLUSIONS"] = json.dumps([{"value": v} for v in roe.exclusions])
            env["ORCHESTRATOR_SEED"] = json.dumps([{"value": v} for v in roe.seed])
        token = JobEnv.bind(env)
        if not self.sandbox:
            return token
        if not self._needs_sandbox(job, self._job_extras(job)):
            if bridge is not None:
                bridge.emit("log", "sandbox skipped (report/bridge-only role tools)")
            return token
        from orchestrator.host.sandbox import ProjectSandbox

        # Provision is idempotent (attach when running); bind per job thread.
        # Serialized per project (across processes with a shared store): sibling jobs starting together would
        # otherwise all try to create the same container and all but one fail with a name conflict.
        with self.store.lock(f"sandbox:{job.project_id}", ttl=600.0, wait=600.0):
            info = ProjectSandbox.provision(
                job.project_id, bind=True, warm_mcp=job.project_id not in self._provisioned,
                socket_dir=self.socket_dir,
            )
            self._provisioned.add(job.project_id)
        if bridge is not None:
            bridge.emit("log", f"sandbox {info.mode}:{info.name} ({info.action})")
            self._provision_clis(job, ws, bridge)
        return token

    def _provision_clis(self, job: JobState, ws: Path, bridge: "LocalAgentBridge") -> None:
        """Install the CLIs the job's roles are allowed to use (tools/catalog cascade)."""
        from orchestrator.crew.roles.registry import RoleRegistry
        from orchestrator.tools.catalog import CatalogProvisioner

        names: list[str] = []
        for rid in job.role_ids:
            role = RoleRegistry.shared().get(rid)
            for cli in (role.allow_binaries if role else ()):
                if cli not in names:
                    names.append(cli)
        if not names:
            return
        try:
            res = CatalogProvisioner.shared().provision(names, workspace=ws)
            if res.installed:
                bridge.emit("log", "provisioned: " + ", ".join(res.installed))
            if res.verified:
                bridge.emit("log", "verified CLIs: " + ", ".join(res.verified))
            if res.errors:
                bridge.emit("error", "CLI provision: " + "; ".join(res.errors))
        except Exception as exc:  # noqa: BLE001
            bridge.emit("error", f"sandbox/provision error: {exc}")

    def execute(self, job_id: str, resume: bool = False, steer: str = "") -> None:
        """Run one job to the end of its turn. Called by the executor (thread or worker).

        Mirrors Peon's ``run_job``: objective checks, dependency/slot gates, RoE fail-closed, the deterministic
        manager bookend, sandbox only for roles that run commands, CLI provisioning, then the agent.
        """
        if not self.store.claim_job(job_id, {"pending"}, "running"):
            logger.info("job %s not pending (already claimed or finished); skipping", job_id)
            return
        self.store.patch_job(job_id, heartbeat=time.time())   # fresh beat now: a long-pending job must not look stale
        job = self.store.get_job(job_id)
        if job is None:
            return
        bridge = LocalAgentBridge(self, job)
        try:  # skills edited on disk since the last run must reach this job's sandbox
            from orchestrator.catalog import PlatformCatalog

            PlatformCatalog.shared().refresh_stale_skills()
        except Exception as exc:  # noqa: BLE001
            logger.warning("skill refresh failed: %s", exc)
        if not job.role_ids and job.objective_seq:
            obj = next((o for o in self.store.get_objectives(job.project_id) if o.seq == job.objective_seq), None)
            if obj is not None and obj.role_id.strip():
                job.role_ids = [obj.role_id.replace(";", ",").split(",")[0].strip()]
                self.store.patch_job(job_id, role_ids=job.role_ids)
        if not job.role_ids:
            self._finish(job_id, bridge, AgentRunResult(ok=False, error="No role_ids on Job"))
            return
        stop = self._objective_gate(job, resume)
        if stop is not None:
            self._finish(job_id, bridge, stop)
            return
        gate = self._gate(job, resume)
        if gate == "deferred":
            return
        if gate == "manager_dispatch":
            self._manager_dispatch(job, bridge)
            return
        block = self._roe_blocks(job)
        if block:
            self._finish(job_id, bridge, AgentRunResult(ok=False, error=block))
            return
        bridge.emit("status", f"Running roles: {', '.join(job.role_ids)}", metadata={"job_status": "running"})
        ws = self.workspace(job.project_id)
        result = AgentRunResult(ok=False, error="not started")
        token = None
        beat_stop = self._start_heartbeat(job_id)
        try:
            token = self._bind_runtime(job, ws, bridge)
            extras = self._job_extras(job)
            scope = JobScope(
                job_id=job.id,
                project_id=job.project_id,
                parent_job_id=job.parent_id,
                workspace=str(ws),
                role_ids=list(job.role_ids),
                brief=job.description or job.title,
                depth=job.depth,
                bridge=bridge,
                extras=extras,
            )
            inbox = bridge.check_inbox() if resume else ""
            ctx = resume_context_text(self, job.id) if resume else ""
            guidance = "\n\n".join(bridge.drain_operator_guidance())
            steer_text = "\n\n".join(
                p for p in (ctx, self._memory_index(job), guidance, steer,
                            inbox if inbox != "Inbox empty." else "") if p
            )
            result = run_agent(scope, self.agent_config, resume=resume, steer=steer_text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("job %s crashed", job.id)
            result = AgentRunResult(ok=False, error=str(exc))
        finally:
            beat_stop.set()
            if token is not None:
                self._release_workspace_ownership(bridge)
            try:
                Session.reset()
                if token is not None:
                    JobEnv.reset(token)
            except Exception:  # noqa: BLE001
                pass
        try:  # skills write ``workspace/findings_queue.jsonl``; ingest what the run left behind
            n = self.findings_service.ingest_workspace_queue(
                job.project_id, ws, job_id=job.id, role_id=(job.role_ids or [""])[0], objective_seq=job.objective_seq)
            if n:
                bridge.emit("log", f"ingested {n} finding(s) from queue")
        except Exception as exc:  # noqa: BLE001
            bridge.emit("error", f"finding ingest: {exc}")
        self._finish(job_id, bridge, result)

    # -- run preparation (Peon: job_run helpers) -----------------------------------------------

    def _objective_gate(self, job: JobState, resume: bool) -> "AgentRunResult | None":
        """A job for a cancelled/already-completed objective does not run (resumes are exempt)."""
        if not job.objective_seq:
            return None
        obj = next((o for o in self.store.get_objectives(job.project_id) if o.seq == job.objective_seq), None)
        if obj is None:
            return None
        if obj.status == "cancelled":
            self.store.patch_job(job.id, control="kill")
            return AgentRunResult(ok=False, error="Objective cancelled")
        if obj.status == "completed" and not resume:
            self.store.patch_job(job.id, control="kill")
            return AgentRunResult(ok=False, error="Objective already completed")
        return None

    def _roe_blocks(self, job: JobState) -> str:
        """Fail-closed: an active probe role with an empty in_scope does not start (only when a RoE exists)."""
        from orchestrator.engagement.roe_ops import roe_block_reason

        roe = self.store.get_project(job.project_id).roe
        if roe is None:
            return ""
        return roe_block_reason(job.role_ids, roe.in_scope) or ""

    def _job_extras(self, job: JobState) -> dict[str, Any]:
        from orchestrator.crew.roles.registry import RoleRegistry
        from orchestrator.host.governance import PROFILE

        extras = {**job.extras, "role_id": (job.role_ids or [""])[0], "skill_ids": list(job.skill_ids)}
        mgr = RoleRegistry.shared().manager()
        # a manager job with no objective is the whole-project crew (the resident governor runs solo)
        if (mgr and extras["role_id"] == mgr.id and not job.objective_seq and job.profile != PROFILE
                and "crew_mode" not in extras):
            extras["crew_mode"] = "project"
        if "dispatch_only" not in extras:
            extras["dispatch_only"] = self._is_dispatch_supervisor(job)
        return extras

    def _is_dispatch_supervisor(self, job: JobState) -> bool:
        """True when this job must delegate execution to spawned workers.

        A specialist (technical role) job that is not itself a spawned worker owns the lane: it plans, spawns
        skill-bound workers and verifies. Children, the PM/analyzer bookends and the governor keep their tools.
        """
        from orchestrator.crew.roles.registry import RoleRegistry
        from orchestrator.host.governance import PROFILE

        if job.parent_id or job.profile == PROFILE:
            return False
        if not settings.get_bool("AGENT_DISPATCH_ONLY", True):
            return False
        if self.agent_config.max_subagents <= 0:
            return False  # nothing could be spawned
        role = RoleRegistry.shared().get(job.role_ids[0]) if job.role_ids else None
        return bool(role is not None and role.is_technical and not role.is_manager and not role.is_analyzer)

    _SANDBOX_COMMAND_TOOLS = frozenset({"bash", "run_skill_script", "run_periodic", "sandbox_status", "provision_cli",
                                        "load_skill", "read_skill_resource"})

    def _needs_sandbox(self, job: JobState, extras: dict[str, Any]) -> bool:
        """True when this job will run commands (those run only in its sandbox)."""
        from orchestrator.crew.roles.registry import RoleRegistry
        from orchestrator.skills.bind import effective_tools, progressive_tool_names, resolve_skill_ids

        reg = RoleRegistry.shared()
        for rid in job.role_ids:
            role = reg.get(rid)
            if role is None:
                return True
            if extras.get("dispatch_only"):
                continue  # a supervisor runs no commands itself: its workers do
            if role.is_technical or role.is_manager or role.allow_binaries:
                return True
            selected = resolve_skill_ids(role, job.skill_ids or None)
            if progressive_tool_names(selected, role_id=rid):
                return True
            surface = set(role.tools or ()) | set(effective_tools(role, selected))
            if self._SANDBOX_COMMAND_TOOLS & surface:
                return True
        return False

    def _memory_index(self, job: JobState) -> str:
        """The memory prelude every agent starts with: what is already known (this project + platform lessons)."""
        try:
            from orchestrator.crew.roles.registry import RoleRegistry
            from orchestrator.memory import store as memory

            rid = (job.role_ids[0] if job.role_ids else "") or ""
            role = RoleRegistry.shared().get(rid) if rid else None
            manager = bool(role and role.kind == "engagement-pm")
            return memory.index_block(job.project_id, role_id=rid, manager=manager)
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory index injection failed: %s", exc)
            return ""

    # -- pre-run gates (Peon: dependency deferral, job slots, deterministic manager bookend) ----

    def _gate(self, job: JobState, resume: bool) -> str:
        """Return ``"run"``, ``"deferred"`` (job was parked/re-queued) or ``"manager_dispatch"``."""
        if job.objective_seq and not resume:
            obj = next((o for o in self.store.get_objectives(job.project_id) if o.seq == job.objective_seq), None)
            if obj is not None and not self.scheduler.dependencies_met(job.project_id, obj):
                # started before its dependencies finished: park as standby, the handoff wakes it
                self.store.patch_job(job.id, idle_note=DEP_NOTE)
                if not self._defer_claim(job, "idle"):
                    self.store.patch_job(job.id, idle_note="")
                return "deferred"
        cap = max(1, settings.get_int("MAX_AGENTS_PER_PROJECT", 8))
        running = [j for j in self.store.list_jobs(job.project_id) if j.status == "running" and j.id != job.id]
        if len(running) >= cap:
            if self._defer_claim(job, "pending"):
                self._redispatch(job.id, resume, delay_ms=5000)
            return "deferred"
        if (not resume and job.objective_seq and self._is_manager(job) and not self._unread(job.id)
                and not self.store.has_guidance(job.id)):
            return "manager_dispatch"
        return "run"

    def _defer_claim(self, job: JobState, to_status: str) -> bool:
        """Park a gated job. If it was cancelled while it sat in the gate, its worker (us) owes the terminal
        bookkeeping that ``cancel`` deliberately left to it."""
        if self.store.claim_job(job.id, {"running"}, to_status):
            return True
        cur = self.store.get_job(job.id)
        if cur is not None and cur.status == "cancelled" and cur.extras.get("terminal_pending"):
            self._finish(job.id, LocalAgentBridge(self, cur), AgentRunResult(ok=False, output="", error="cancelled"))
        return False

    def _release_workspace_ownership(self, bridge: "LocalAgentBridge | None" = None) -> None:
        """Give the bind-mounted workspace back to the host user.

        The sandbox runs as root, so everything an agent wrote (venvs, pip installs, artifacts) is root-owned on the
        host and cannot be edited or deleted without sudo. Chowning at the end of each job keeps agents root (apt and
        CLI provisioning need it) and the host's files theirs. ``SANDBOX_CHOWN=false`` turns it off;
        ``SANDBOX_HOST_UID`` / ``SANDBOX_HOST_GID`` override the target (default: this process's user, skipped for root).
        """
        if not settings.get_bool("SANDBOX_CHOWN", True):
            return
        uid = settings.get_int("SANDBOX_HOST_UID", os.getuid() if hasattr(os, "getuid") else 0)
        gid = settings.get_int("SANDBOX_HOST_GID", os.getgid() if hasattr(os, "getgid") else 0)
        if uid <= 0:
            return   # a root host process owns nothing it needs to reclaim
        try:
            res = Session.current().exec(["chown", "-R", f"{uid}:{gid}", "/workspace"], timeout=120)
            if not res.ok and bridge is not None:
                bridge.emit("log", f"workspace chown skipped: {res.stderr[:120]}")
        except Exception as exc:  # noqa: BLE001 — cosmetic: never fail a job over file ownership
            logger.debug("workspace chown failed: %s", exc)

    def _redispatch(self, job_id: str, resume: bool, *, delay_ms: int) -> None:
        try:
            self.executor.dispatch(job_id, resume, "", delay_ms=delay_ms)
        except TypeError:  # an executor without delay support
            self.executor.dispatch(job_id, resume, "")

    def _is_manager(self, job: JobState) -> bool:
        from orchestrator.crew.roles.registry import RoleRegistry

        role = RoleRegistry.shared().get(job.role_ids[0]) if job.role_ids else None
        return bool(role and role.kind == "engagement-pm" and not job.extras.get("crew_mode"))

    def _manager_dispatch(self, job: JobState, bridge: "LocalAgentBridge") -> None:
        """The manager bookend of a planned project: complete deterministically (no LLM turn);
        the scheduler releases the ready objectives. Replan/steer/messages still use the LLM path."""
        note = "Manager bookend: dispatch next ready objective (deterministic, no LLM)"
        bridge.emit("status", note, metadata={"event": "manager_dispatch"})
        self._finish(job.id, bridge, AgentRunResult(ok=True, output=f"## {job.role_ids[0]}\nok=True\n{note}"))

    # -- liveness ----------------------------------------------------------------------

    def _start_heartbeat(self, job_id: str) -> threading.Event:
        """Beat ``heartbeat`` on the job every ``JOB_HEARTBEAT_S`` seconds until the returned event is set."""
        stop = threading.Event()
        interval = max(0.05, settings.get_float("JOB_HEARTBEAT_S", 10.0))
        self.store.patch_job(job_id, heartbeat=time.time())

        def beat() -> None:
            while not stop.wait(interval):
                try:
                    self.store.patch_job(job_id, heartbeat=time.time())
                except Exception:  # noqa: BLE001 — a missed beat is retried next tick
                    logger.debug("heartbeat for %s failed", job_id, exc_info=True)

        threading.Thread(target=beat, name=f"hb-{job_id}", daemon=True).start()
        return stop

    def reap_stale(self, *, timeout: float | None = None, max_attempts: int | None = None) -> list[str]:
        """Recover jobs whose worker died: ``running`` with no heartbeat for ``timeout`` seconds.

        A job is resumed (``pending`` again, ``resume=True`` so it continues from its last work) up to
        ``max_attempts`` times, then failed with ``worker lost`` — and its parent gets the usual handoff.
        Run this periodically (the scheduler service does). Returns the ids it acted on.
        """
        timeout = settings.get_float("JOB_STALE_S", 90.0) if timeout is None else timeout
        limit = settings.get_int("JOB_MAX_ATTEMPTS", 2) if max_attempts is None else max_attempts
        now = time.time()
        acted: list[str] = []
        for pid in self.store.list_projects():
            for job in self.store.list_jobs(pid):
                if job.status != "running" or now - (job.heartbeat or job.created_at) <= timeout:
                    continue
                attempts = int(job.extras.get("attempts", 1))
                if attempts < limit:
                    if self.store.claim_job(job.id, {"running"}, "pending"):
                        self.store.patch_job(job.id, extras={**job.extras, "attempts": attempts + 1}, control="",
                                             heartbeat=time.time())
                        self.executor.dispatch(job.id, True, "")
                        acted.append(job.id)
                elif self.store.claim_job(job.id, {"running"}, "failed"):
                    self.store.patch_job(job.id, error=f"worker lost (no heartbeat for {int(timeout)}s)")
                    lost = self.store.get_job(job.id)
                    if lost is not None:
                        self._record_run(lost)
                        self._handoff_to_parent(lost)
                    acted.append(job.id)
        if acted:
            logger.warning("reaped stale jobs: %s", acted)
        return acted

    def _finish(self, job_id: str, bridge: LocalAgentBridge, result: AgentRunResult) -> None:
        job = self.store.get_job(job_id)
        if job is not None and job.status == "cancelled" and job.extras.get("terminal_pending"):
            # cancelled while running: its worker owns the terminal bookkeeping (checkpoint, handoff, scheduler)
            self.store.patch_job(job_id, extras={k: v for k, v in job.extras.items() if k != "terminal_pending"},
                                 result=(result.output or job.result)[:200_000], control="")
            self._after_terminal(self.store.get_job(job_id) or job, bridge)
            return
        if job is None or job.status == "cancelled":
            return
        patch: dict[str, Any] = {"result": (result.output or job.result)[:200_000]}
        if job.control == "pause":
            # the operator paused a running job: keep its work, park it resumable
            self.store.patch_job(job_id, **patch, status="paused")
            write_checkpoint(self, self.store.get_job(job_id) or job, reason="paused")
            bridge.emit("status", "Job → paused", metadata={"job_status": "paused"})
            return
        if job.control == "kill":
            self.store.patch_job(job_id, **patch, status="cancelled",
                                 error=job.error if job.error in {"Objective cancelled", "Objective already completed"} else "Cancelled")
            self._after_terminal(self.store.get_job(job_id) or job, bridge)
            return
        if result.ok and job.profile == "governance":
            self.store.patch_job(job_id, **patch)
            self.governance.repark(job_id)  # the resident governor never completes; it re-parks
            return
        if (result.ok and job.extras.get("dispatch_only") and not job.profile and not bridge.idle_requested
                and not self._has_children(job)):
            # a dispatch-only supervisor runs no commands itself: ending without any worker means nothing was done
            if not job.extras.get("dispatch_nudged"):
                self.store.patch_job(job_id, **patch, extras={**job.extras, "dispatch_nudged": True})
                self.store.add_guidance(
                    job_id, "You are a dispatch-only supervisor and spawned no worker, so nothing has been done. "
                    "Use spawn_work to delegate this objective to workers, wait_for_work for their results, then finish.")
                bridge.emit("log", "dispatch supervisor finished without spawning a worker — nudged to delegate")
                if self.store.claim_job(job_id, {"running"}, "pending"):
                    self.executor.dispatch(job_id, True, "")
                return
            result = AgentRunResult(ok=False, output=result.output,
                                    error="dispatch supervisor finished without spawning any worker")
            patch["result"] = (result.output or job.result)[:200_000]
        unread_runs = int(job.extras.get("unread_resumes", 0))
        if (result.ok and not job.profile and not bridge.idle_requested and unread_runs < 3
                and self._unread(job.id)):
            # mail arrived while this turn ran and the agent never read it: take one more turn (bounded, so an
            # agent that ignores its inbox cannot loop forever)
            self.store.patch_job(job_id, **patch, extras={**job.extras, "unread_resumes": unread_runs + 1})
            if self.store.claim_job(job_id, {"running"}, "pending"):
                self.executor.dispatch(job_id, True, "")
            return
        if result.ok and bridge.idle_requested and self._unread(job.id):
            # A message arrived while this turn was running (its wake found the job "running"
            # and was a no-op): resume right away instead of parking on a message already sent.
            self.store.patch_job(job_id, **patch)
            if self.store.claim_job(job_id, {"running"}, "pending"):
                self.executor.dispatch(job_id, True, "")
            return
        if result.ok and bridge.idle_requested and self.other_alive(job):
            patch.update(status="idle", idle_note=bridge.idle_note)
        else:
            if result.ok and bridge.idle_requested:
                bridge.emit("log", "go_idle ignored: no other agent is alive to message this one — finishing.")
            patch.update(status="completed" if result.ok else "failed",
                         error="" if result.ok else (result.error or "agent failed")[:2000])
        self.store.patch_job(job_id, **patch)
        if patch.get("status") == "idle" and self._unread(job_id):
            self.wake(job_id)  # a message landed between the check and parking
        job = self.store.get_job(job_id) or job
        bridge.emit("status", f"Job → {job.status}", metadata={"job_status": job.status})
        if job.status == "idle":
            try:
                write_checkpoint(self, job, reason="idle")
            except Exception:  # noqa: BLE001
                logger.warning("checkpoint failed for %s", job.id, exc_info=True)
        if job.status in TERMINAL:
            self._after_terminal(job, bridge)

    def _has_children(self, job: JobState) -> bool:
        return any(j.parent_id == job.id for j in self.store.list_jobs(job.project_id))

    def _after_terminal(self, job: JobState, bridge: "LocalAgentBridge | None" = None) -> None:
        """Everything that follows a job reaching a terminal state (Peon: ``finish_job``)."""
        bridge = bridge or LocalAgentBridge(self, job)
        try:
            write_checkpoint(self, job, reason="job_finished")
        except Exception:  # noqa: BLE001
            logger.warning("checkpoint failed for %s", job.id, exc_info=True)
        self._record_run(job)
        bridge.append_transcript(
            record_from_event(
                category="error" if job.status == "failed" else "outcome",
                summary=f"job → {job.status}" + (f": {job.error[:400]}" if job.error else ""),
                project_id=job.project_id,
                objective_id=str(job.objective_seq or ""),
                job_id=job.id,
                role_id=(job.role_ids or [""])[0],
                event=EVENT_WORK_FINISHED,
                payload={"status": job.status, "parent_job_id": job.parent_id},
            )
        )
        if job.error:
            bridge.emit("error", job.error)
        self._handoff_to_parent(job)
        if job.status in {"failed", "cancelled"}:  # orphaned children do not outlive their parent
            for child in self.store.list_jobs(job.project_id):
                if child.parent_id == job.id and child.status not in TERMINAL:
                    self.cancel(child.id)   # orphans do not outlive the parent (objective status is left alone)
        try:
            self.scheduler.on_job_finished(job)
        except Exception:  # noqa: BLE001 — scheduling trouble must not fail the finished job
            logger.exception("scheduler hook failed for job %s", job.id)
        if job.status == "completed" and job.parent_id:
            try:  # a finished child may have added evidence: let the analyzer rewrite its report
                self.scheduler.maybe_reopen_analyzer_after_child(job)
            except Exception:  # noqa: BLE001
                logger.exception("analyzer reopen failed for job %s", job.id)

    def interim_backstop(self, job_id: str) -> bool:
        """Coalescing backstop for silent (``wake=None``) messages: once ``A2A_INTERIM_WAKE_UNREAD`` of them pile up
        unread on an idle agent, wake it anyway so streamed work is not stranded until the producer finishes.
        The governor (escalation-only) and dependency standbys (scheduler-owned) are exempt. ``0`` disables."""
        job = self.store.get_job(job_id)
        if job is None or job.status != "idle" or job.profile == "governance" or job.idle_note == DEP_NOTE:
            return False
        threshold = settings.get_int("A2A_INTERIM_WAKE_UNREAD", 3)
        if threshold <= 0:
            return False
        unread = sum(1 for m in self.hub.inbox(job_id, unread_only=True) if m.recipient_job_id == job_id)
        if unread < threshold:
            return False
        self.wake(job_id)
        return True

    def _unread(self, job_id: str) -> bool:
        return any(m.recipient_job_id == job_id for m in self.hub.inbox(job_id, unread_only=True))

    _BOOKEND_NOISE = "Manager bookend: dispatch next ready objective"

    @classmethod
    def _result_excerpt(cls, job: JobState, limit: int) -> str:
        text = (job.result or "").strip()
        return "" if cls._BOOKEND_NOISE in text else text[:limit]

    def _parent_handoff_text(self, child: JobState) -> str:
        who = ((child.role_ids[0] if child.role_ids else "") or child.title or "sub-agent")
        verb = {"completed": "completed", "failed": "failed", "cancelled": "was cancelled"}.get(child.status, child.status)
        lines = [f"Sub-agent {who} ({child.title}) {verb}."]
        if child.status == "failed" and (child.error or "").strip():
            lines.append("Error:\n" + child.error.strip()[:1800])
        result = self._result_excerpt(child, 1800)
        if result:
            lines.append("Result:\n" + result)
        lines.append("Use `list_findings` / `list_workspace_artifacts` for its findings and files.")
        return "\n\n".join(lines)

    def _handoff_to_parent(self, job: JobState) -> None:
        """Push a terminal child's result to its parent, waking it only if parked (A2A handoff, one per child).

        A running parent is not woken (it reads the handoff on its next ``check_inbox``); a terminal parent is
        skipped — a late child must not resurrect it.
        """
        parent = self.store.get_job(job.parent_id) if job.parent_id else None
        if parent is None or parent.status in TERMINAL:
            return
        if any(m.kind == "handoff" and m.sender_job_id == job.id for m in self.hub.inbox(parent.id)):
            return  # one handoff per child
        try:
            self.hub.send(sender_job_id=job.id, recipient_job_id=parent.id, kind="handoff",
                          content=self._parent_handoff_text(job), wake=self.wake)
        except ValueError as exc:
            logger.debug("parent handoff skipped: %s", exc)

    def _record_run(self, job: JobState) -> None:
        obj = next((o for o in self.store.get_objectives(job.project_id) if o.seq == job.objective_seq), None)
        roe = self.store.get_project(job.project_id).roe
        events = (self.store.get_job(job.id, events=True) or job).events
        try:
            self.run_log.write(
                RunRecord(
                    job_id=job.id,
                    project_id=job.project_id,
                    objective_id=str(job.objective_seq or ""),
                    title=job.title,
                    description=job.description,
                    role_ids=list(job.role_ids),
                    skill_ids=list(job.skill_ids),
                    status=job.status,
                    error=job.error,
                    objective_title=obj.title if obj else "",
                    events=[
                        {"message_type": e["message_type"], "content": e["content"], "metadata": e["metadata"]}
                        for e in events
                    ],
                    scope_values=(roe.in_scope + roe.seed) if roe else [],
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("run log write failed: %s", exc)

    def shutdown(self, wait: bool = True) -> None:
        self.stop_stream_server()
        if hasattr(self.executor, "shutdown"):
            self.executor.shutdown(wait=wait)
