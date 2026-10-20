"""Operator console chat.

One free-text operator message is routed to ``answer`` (project Q&A), ``instruct`` (steer live agents),
``replan`` (revise the objective graph) or ``stop`` (pause the project). A message addressed to one agent
(``job_id``) is a direct conversation with that agent and never changes the plan. A pending operator prompt
is answered by a non-question message.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from orchestrator.engagement.roe_ops import authorize_operator_scope
from orchestrator.host.state import TERMINAL, JobState
from orchestrator.utils.llm import LLM_NOT_CONFIGURED, chat_json, chat_text, llm_configured

if TYPE_CHECKING:
    from orchestrator.host.control import LocalControlPlane

logger = logging.getLogger(__name__)

_ROUTE_SYSTEM = """Classify one operator message for a project console.
Return ONLY compact JSON:
{"intent":"answer"|"instruct"|"replan"|"stop","reason":"short"}

intents:
- answer — wants information about THIS project (status, findings, objectives, scope/scope rules, what agents did).
- instruct — guidance, correction, priority, constraint or tactical suggestion that live agents can apply
  while continuing the current objective graph.
- replan — explicitly requests a replan, adds/removes objectives or targets, changes the goal/scope, or
  requests work not represented by the current objective graph.
- stop — pause / halt the project.

If both a question and an action appear, prefer instruct or replan. If unsure between answer and action,
prefer answer only when no action is requested."""

_ASK_SYSTEM = """You are the project console assistant.
Answer using only the provided project context. Be concise and concrete.
Do not invent findings, hosts, or results. If the context lacks the answer, say so.
Do not start new work; for new work the operator should send a work request (the console will replan)."""

_AGENT_SYSTEM = """You are one agent on a project, answering the operator who is talking to YOU directly.
Speak in the first person as that agent. Answer using only the context provided about your own work:
your objective, what you ran, what you found, what you are waiting for. Be concise and concrete.
Do not invent results. You cannot take new actions in this reply — if the operator wants you to do something,
tell them to send it as an instruction and you will pick it up."""

_LIVE = {"running", "pending", "paused", "idle"}


class OperatorConsole:
    def __init__(self, plane: "LocalControlPlane") -> None:
        self.plane = plane
        self.store = plane.store
        self.ops = plane.operator_control

    # ── context ───────────────────────────────────────────────────────────────

    def _roots(self, project_id: str) -> list[JobState]:
        return [j for j in self.store.list_jobs(project_id) if not j.parent_id and j.profile != "governance"]

    def has_live_agents(self, project_id: str) -> bool:
        return any(j.status in _LIVE for j in self._roots(project_id))

    def objective_lines(self, project_id: str, limit: int = 12) -> list[str]:
        rows = [o for o in self.store.get_objectives(project_id) if o.status != "cancelled"]
        return [f"• OBJ-{o.seq} [{o.status}] {o.title}" for o in sorted(rows, key=lambda o: o.seq)[:limit]]

    def project_brief(self, project_id: str) -> str:
        meta = self.store.get_project(project_id)
        objs = sorted(self.store.get_objectives(project_id), key=lambda o: o.seq)
        findings = self.store.get_findings(project_id)[-15:]
        live = sum(1 for j in self._roots(project_id) if j.status in _LIVE)
        lines = [
            f"Project: {meta.title or project_id}",
            f"Status: {self.plane.scheduler.project_status(project_id)} (project {meta.status})",
            f"Brief: {(meta.summary or '').strip() or '(none)'}",
            f"Live root jobs: {live}",
            f"Findings: {len(findings)} (showing up to 15)",
            f"Objectives: {len(objs)}",
        ]
        roe = meta.roe
        if roe is not None and roe.in_scope:
            vals = [str(r.get("value") or r) if isinstance(r, dict) else str(r) for r in roe.in_scope[:12]]
            lines.append("In-scope: " + ", ".join(v for v in vals if v.strip()))
        for o in objs[:20]:
            extra = f" — blocked: {o.note}" if o.status == "blocked" and (o.note or "").strip() else ""
            lines.append(f"- OBJ-{o.seq} [{o.status}] {o.title}{extra}")
        lines.append(f"Objectives completed: {sum(1 for o in objs if o.status == 'completed')}/{len(objs)}")
        for f in findings:
            lines.append(f"- Finding [Severity {f.get('severity', '')}/{f.get('kind', '')}] "
                         f"{(f.get('title') or '').strip() or '(untitled)'}")
        return "\n".join(lines)

    def agent_label(self, job: JobState) -> str:
        from orchestrator.crew.roles.registry import RoleRegistry

        rid = (job.role_ids[0] if job.role_ids else "").strip()
        role = RoleRegistry.shared().get(rid) if rid else None
        return (getattr(role, "label", "") or rid or job.title or "agent").strip()

    def agent_context(self, project_id: str, job: JobState) -> str:
        from orchestrator.crew.roles.registry import RoleRegistry

        rid = (job.role_ids[0] if job.role_ids else "").strip()
        role = RoleRegistry.shared().get(rid) if rid else None
        lines = [
            f"You are: {self.agent_label(job)} (role {rid or '-'})",
            f"Your goal: {(getattr(role, 'goal', '') or '').strip() or '(none recorded)'}",
            f"Your task: {job.title}",
            f"State: {job.status}",
        ]
        if job.status == "idle":
            lines.append(f"You are idle, waiting for: {job.idle_note or 'a message'}")
        if job.error:
            lines.append(f"Last error: {job.error[:500]}")
        obj = next((o for o in self.store.get_objectives(project_id) if o.seq == job.objective_seq), None)
        if obj is not None:
            lines += [f"Objective OBJ-{obj.seq} [{obj.status}]: {obj.title}",
                      f"Objective detail: {(obj.description or '').strip()[:800]}",
                      f"Acceptance: {(obj.acceptance or '').strip()[:400]}"]
        if (job.result or "").strip():
            lines.append("Your result so far:\n" + job.result.strip()[:2500])
        full = self.store.get_job(job.id, events=True)
        events = [e for e in (full.events if full else []) if e.get("type") != "thinking"][-30:]
        if events:
            lines.append("Your recent activity (oldest first):")
            lines += [f"- [{e.get('type', '')}] {str(e.get('content') or e.get('text') or '')[:220]}" for e in events]
        return "\n".join(lines)

    # ── routing ───────────────────────────────────────────────────────────────

    def fallback_intent(self, project_id: str, text: str) -> str:
        n = " ".join((text or "").strip().lower().split())
        if n in {"stop", "pause", "halt", "stop project", "pause project"}:
            return "stop"
        if n.endswith("?"):
            return "answer"
        return "instruct" if self.has_live_agents(project_id) else "replan"

    def classify(self, project_id: str, text: str) -> str:
        if not llm_configured():
            return self.fallback_intent(project_id, text)
        try:
            data = chat_json(
                _ROUTE_SYSTEM,
                f"Project context:\n{self.project_brief(project_id)}\n\n"
                f"Live agents: {'yes' if self.has_live_agents(project_id) else 'no'}\n\nOperator message:\n{text}")
        except Exception:  # noqa: BLE001
            logger.exception("console chat route failed")
            return self.fallback_intent(project_id, text)
        intent = str((data or {}).get("intent") or "answer").strip().lower()
        if intent == "work":
            intent = "replan"
        return intent if intent in {"answer", "instruct", "replan", "stop"} else "answer"

    def first_open_prompt(self, project_id: str) -> dict[str, Any] | None:
        return next((p for p in self.store.get_prompts(project_id) if not p.get("answer")), None)

    # ── actions ───────────────────────────────────────────────────────────────

    def _remember(self, project_id: str, text: str, source: str) -> None:
        self.plane._remember_feedback(project_id, text, source=source)

    def replan(self, project_id: str, text: str) -> dict[str, Any]:
        if not llm_configured():
            raise RuntimeError(LLM_NOT_CONFIGURED)
        authorize_operator_scope(self.plane, project_id, text)   # operator-named assets are authorized, not expanded
        self._remember(project_id, text, "replan request")
        result = self.ops.replan(project_id, text)
        n = int(result.get("objectives") or len(self.store.get_objectives(project_id)))
        lines = [f"Got it — I'm replanning from your request (“{text[:120]}”). {n} objective(s) are on the board:"]
        lines += self.objective_lines(project_id) or ["• (objectives are still syncing — check again in a moment)"]
        lines += ["", "Agents will pick up the next ready objective. Ask me anytime for status or to change direction."]
        return {"ok": True, **result, "reply": "\n".join(lines)}

    def instruct(self, project_id: str, text: str, *, job_id: str | None = None) -> dict[str, Any]:
        """Steer live agents only; with none live, escalate to a replan."""
        if not job_id and not self.has_live_agents(project_id):
            return self.replan(project_id, text)
        self.ops._allow_operator(project_id)
        target = self._target(project_id, job_id) if job_id else None
        if target is not None and target.status in TERMINAL:
            raise ValueError(f"{self.agent_label(target)} has finished ({target.status}) and cannot take new "
                             "instructions. Ask it about its work, or send a replan for new work.")
        self._remember(project_id, text, "instruction")
        try:
            result = self.ops.instruct(project_id, text, job_id=job_id)
        except RuntimeError as exc:
            if "No running agents" in str(exc):
                return self.replan(project_id, text)
            raise
        if target is not None:
            now = self.store.get_job(target.id) or target
            state = {"pending": "It was idle and has been woken — it will pick this up now.",
                     "running": "It is working; it will see this at its next inbox check.",
                     "paused": "It is paused; it will see this when resumed.",
                     "idle": "It is waiting on its dependencies and will see this when it starts."}.get(now.status, "")
            reply = f"Sent to {self.agent_label(now)}. {state}".strip()
            result = {**result, "agent": {"id": now.id, "label": self.agent_label(now), "status": now.status}}
        else:
            reply = (f"Instructed {len(result.get('job_ids') or [])} live agent(s) with your note. "
                     "They will adjust under scope rules — ask me for a status update anytime.")
        return {"ok": True, "mode": "instruct", **result, "reply": reply}

    def answer(self, project_id: str, text: str) -> dict[str, Any]:
        if not llm_configured():
            reply = f"{LLM_NOT_CONFIGURED} Project status: {self.plane.scheduler.project_status(project_id)}."
        else:
            try:
                reply = chat_text(_ASK_SYSTEM, f"Context:\n{self.project_brief(project_id)}\n\nQuestion:\n{text}"
                                  ).strip() or "I could not produce an answer."
            except Exception as exc:  # noqa: BLE001
                logger.exception("console ask failed")
                reply = f"Could not answer right now: {exc}"
        return {"ok": True, "mode": "answer", "reply": reply}

    def ask_agent(self, project_id: str, text: str, job: JobState) -> dict[str, Any]:
        """Conversational answer from one agent, without disturbing its work."""
        label = self.agent_label(job)
        if not llm_configured():
            reply = f"{LLM_NOT_CONFIGURED} {label} is {job.status}" + (f" — {job.idle_note}" if job.idle_note else "") + "."
        else:
            try:
                reply = chat_text(_AGENT_SYSTEM, f"Context about your work:\n{self.agent_context(project_id, job)}\n\n"
                                  f"Operator asks you:\n{text}").strip() or "I could not produce an answer."
            except Exception as exc:  # noqa: BLE001
                logger.exception("console agent ask failed")
                reply = f"Could not answer right now: {exc}"
        return {"ok": True, "mode": "agent_answer", "reply": reply,
                "agent": {"id": job.id, "label": label, "status": job.status}}

    def stop(self, project_id: str, text: str = "") -> dict[str, Any]:
        if self.store.get_project(project_id).status == "stopped":
            raise RuntimeError("Project is cancelled — cannot stop")
        self.plane.pause_project(project_id)
        return {"ok": True, "mode": "stop", "project_status": "paused", "job_ids": [],
                "reply": "Project paused — resume when you want to continue."}

    def _target(self, project_id: str, job_id: str) -> JobState:
        job = self.store.get_job(str(job_id).strip())
        if job is None or job.project_id != project_id:
            raise ValueError("That agent is not on this project")
        return job

    # ── entry point ───────────────────────────────────────────────────────────

    def chat(self, project_id: str, message: str, *, mode: str = "chat", job_id: str | None = None,
             prompt_id: str | None = None) -> dict[str, Any]:
        self.ops.require_project(project_id)
        m = (mode or "chat").strip().lower()
        m = "chat" if m in {"auto", "ask"} or m not in {"chat", "instruct", "replan", "stop", "reply"} else m
        text = (message or "").strip()
        if m == "stop":
            return self.stop(project_id, text)
        if not text:
            raise ValueError("Message is required")
        if prompt_id:
            if not any(p["id"] == prompt_id and not p.get("answer") for p in self.store.get_prompts(project_id)):
                raise RuntimeError("Pending prompt not found or already answered")
            return self._reply(project_id, prompt_id, text)
        if m == "reply":
            pending = self.first_open_prompt(project_id)
            if pending is None:
                raise RuntimeError("No pending operator prompt to answer")
            return self._reply(project_id, pending["id"], text)
        if m == "chat" and job_id:
            return self.ask_agent(project_id, text, self._target(project_id, job_id))
        if m == "chat":
            pending = self.first_open_prompt(project_id)
            if pending is not None and not llm_configured() and not text.rstrip().endswith("?"):
                return self._reply(project_id, pending["id"], text)   # no classifier: a plain line answers the prompt
            intent = self.classify(project_id, text)
            if pending is not None and intent == "answer" and not text.rstrip().endswith("?"):
                return self._reply(project_id, pending["id"], text)
            if intent == "stop":
                return self.stop(project_id, text)
            if intent == "instruct":
                return self.instruct(project_id, text)
            if intent == "replan":
                return self.replan(project_id, text)
            return self.answer(project_id, text)
        if m == "replan":
            return self.replan(project_id, text)
        return self.instruct(project_id, text, job_id=job_id)

    def _reply(self, project_id: str, prompt_id: str, text: str) -> dict[str, Any]:
        ok = self.plane.reply_to_prompt(project_id, prompt_id, text)
        return {"ok": bool(ok), "mode": "reply", "prompt_id": prompt_id, "reply": "Reply delivered." if ok else "Prompt not found."}
