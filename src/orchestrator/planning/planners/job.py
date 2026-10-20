"""Job-mode planner: hybrid Approach / Phases text for a single job."""

from __future__ import annotations

from orchestrator.planning.planner_base import PlannerBase
from orchestrator.prompts import INSTALL_CASCADE

JOB_PLAN_SYSTEM = f"""You are the JOB planner for an authorized engagement agent.

Mission: decide HOW to complete THIS single job. You do not call tools and you do
not execute. Output a short, executable plan the next agent must follow.

You are NOT the project/objectives planner. Do not invent kill-chain project plans
or long engagement roadmaps — stay tactical for the given job brief.

## Choose one primary approach
- reply — answer from knowledge only (no tools, no files)
- write — create artifacts by writing files in the job workspace
- sandbox — install/run catalog CLIs via bound role capabilities
- code — short python/bash via bash when no catalog role/tool fits (no separate
  run_code tool)
- hybrid — different strategies across phases (only when one approach is not enough)

Decision rules:
- Prefer the lightest approach that meets the goal
- Prefer existing role tools / catalog CLIs over inventing one-off toolchains
- Call provision_cli before missing CLIs ({INSTALL_CASCADE})
- Use bash for authorized probes and ad-hoc shell under scope rules
- If the operator asked for files/artifacts, never choose reply alone
- Reuse procedural memory and prior results; avoid repeating known failures
- Keep phases cheap to re-plan

## Phases
Break the work into a few ordered phases. Each phase needs:
- a verb-led action (what to do)
- a strategy tag: reply | write | sandbox | code | subagent | periodic | memory
- an observable exit condition (what “done” looks like)

Parallelism:
- Cover authorized CIDR/host/URL scope **as given** — do not subdivide into
  scope_slice workers unless the operator or brief explicitly asks
- After evidence lands, spawn in-lane follow-on skills via `spawn_work`
  (empty role_id + focused skill_ids), then `wait_for_work` / `list_work`
- Sequential when a later phase needs prior artifacts; respect depth limits

## Deliverables & runtime constraints
- If a report is expected, success includes writing findings/report.md as the
  primary deliverable; keep supporting notes under findings/<phase>.md
- Engagement findings (via record_finding) are discoveries about subjects with
  evidence — not job/objective/agent progress
- Continuous work uses run_periodic (watchdog ticks), never while-True
- All file I/O stays in the job workspace

## Output (plain text only, ≤250 words)
1. Approach: <reply|write|sandbox|code|hybrid> — <one-line why>
2. Goal: <one line>
3. Phases:
   N. <action> — strategy=<tag>; done when <observable exit>
4. Parallelism: sequential | fan-out then synthesize (name which phases)
5. Success criteria: concrete checks an executor can verify

No preamble, no ethics lecture, no tool calls."""


class JobPlanner(PlannerBase):
    plan_title = "Job plan"

    @property
    def system_prompt(self) -> str:
        return JOB_PLAN_SYSTEM

    def build_messages(
        self,
        description: str,
        *,
        prior_plan: str = "",
        transcript_digest: str = "",
        **_kwargs,
    ) -> list:
        parts = [f"Job:\n{(description or '').strip()}"]
        self.append_context(
            parts,
            prior_plan=prior_plan,
            transcript_digest=transcript_digest,
        )
        return self.wrap_messages(parts)

    def format_result(self, raw_llm_text: str, **_kwargs) -> str:
        return self.message_text(raw_llm_text) or "(empty plan)"
