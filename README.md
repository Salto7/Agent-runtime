# orchestrator

A stand-alone, host-agnostic multi-agent runtime. It runs
[CrewAI](https://crewai.com) crews (a manager, specialists and an analyzer) on top of
[LiteLLM](https://litellm.ai), executes their work in per-project Docker sandboxes, lets agents
delegate, message each other (A2A), remember (Cognee) and *dream* (turn finished runs into lessons),
and lets operators grow the system with skills, CLI recipes and MCP servers — all as data.

Use it on its own or embed it in a larger application. Scope rules (see below) are **optional**: a project without
them is unrestricted.

## What is in the box

| Capability | Where | Notes |
|---|---|---|
| **LLM layer** | `orchestrator.llm`, `utils.llm`, `utils.llm_usage` | LiteLLM in-process (any provider via env); per-call cost/token telemetry with `llm_usage.purpose(...)`; optional OpenAI-compatible **LiteLLM proxy** lifecycle (`llm.proxy.LlmProxy`) |
| **CrewAI crews** | `orchestrator.crew` | One role = one CrewAI agent (`CrewAIJobRuntime`); a project = hierarchical crew manager → specialists → analyzer (`ProjectCrewRuntime`); role/skill/tool binding, RoleRouter |
| **Execution runtime** | `agent_runtime`, `host.sandbox` | Docker (and OpenShell) sandbox per project, shell/PTY, bind-mounted workspace, base-command cache |
| **Durable execution** | `orchestrator.queue`, `docker-compose.yml` | **Dramatiq** workers on **Redis**: jobs, findings, objectives, A2A messages and guidance live in Redis, so any process can submit/observe and any worker can run or wake a job; periodic dream scheduler |
| **Objective scheduler** | `host.scheduler` | Plan → objectives with `depends_on`; ready objectives release in parallel, the rest get an idle *standby* agent that is woken with its dependencies' results as A2A handoffs; failed objectives **replan** (up to `AGENT_MAX_FAILURE_REPLANS`) then raise an operator prompt; per-project job slots (`MAX_AGENTS_PER_PROJECT`) |
| **Checkpoints, resume, pause** | `host.checkpoints`, `host.control` | A checkpoint is written when a job finishes, idles or pauses; a message to a *finished* agent resumes it from its checkpoint (failed/cancelled agents stay stopped); `pause` / `resume` / `kill` for jobs and projects (a running job is asked to stop: its next command or spawn is refused and the turn ends `paused`) |
| **Durable projects & schedules** | `host.schedules` | `start_project(kind="durable")` waits for `accept_plan`; `create_schedule` (cron / interval / once → a role job, or an objective re-run) is fired by the scheduler service; `skip_if_running`; paused/stopped projects don't fire; between cycles a durable project is `idle` |
| **Operator control & console** | `host.operator`, `host.console`, `Orchestrator.chat` | `instruct_project` / `replan_project` (`REPLAN_MODE=manager|plan`) / `restart_project` / `stop_project` / `resume_project`; `chat(project_id, text, job_id=None)` routes free text to answer / instruct / replan / stop, or asks one agent about its work; operator-named assets are added to the scope rules when the project has them |
| **Governance & findings** | `host.governance`, `engagement` | Resident governance PM per project; optional scope rules (scope, exclusions, candidates), finding normalizer/dedup, asset graph, queue-file finding ingest; stream sockets from sandbox tools |
| **Config** | `.env`, `.env.example` | `.env` in the repo root is loaded at startup (never overrides real env); `.env.example` lists every option |
| **Delegation** | `agent.delegation`, `host.control` | `spawn_work` (child / peer, depth + fan-out caps), `wait_for_work`, `propose_work`, scope slices checked against the scope rules when set |
| **A2A** | `brain.A2AHub`, `host.control` | Directed messages with per-pair and recipient caps, idle/wake (a finished child wakes its parent), inbox, delivery status; silent messages are coalesced — `A2A_INTERIM_WAKE_UNREAD` of them piled on an idle agent wake it anyway (governor and dependency standbys exempt) |
| **Memory lifecycle** | `host.control`, `host.scheduler`, `api` | Every job starts with the memory index (project notes + platform lessons); a new project recalls earlier lessons into `memory_bootstrap.md`; a project digest is saved when it finishes (or `stop_project`); operator steers and typed prompt replies are captured as durable feedback |
| **Memory** | `orchestrator.memory`, `crew.memory`, `crew.cognee_backend` | Typed notes (SQLite source of truth, BM25 recall) mirrored to **Cognee** over HTTP; platform lessons are guarded against leaking targets |
| **Dreaming** | `crew.dreaming`, `crew.gist` | Curate finished runs into gists → synthesize playbooks → *propose* role/skill changes. Proposals are staged; only an explicit `apply_proposal(..., decision="approve")` changes the catalog |
| **Toolsmith / Learn** | `orchestrator.learn`, `tools` | Draft a CLI recipe or skill from a prompt, lint it, and install-test it in a throwaway lab container before use; install cascade catalog → skill docs → LLM |
| **Catalog** | `orchestrator.catalog` | Roles, skills, MCP servers and role bindings in one SQLite file (replaces Django models) |
| **Skills / MCP / Marketplace** | `skills`, `mcp`, `marketplace` | Agent Skills (`SKILL.md`) + native extensions, stdio MCP servers run in the sandbox, read-only marketplace scanner |

## Install

```bash
pip install git+https://github.com/Salto7/Agent-runtime.git     # or: pip install -e .
cp .env.example .env                                            # set OPENROUTER_API_KEY (or another provider key);
                                                                # the library loads <project root>/.env automatically (real env vars win)
docker compose --profile build build sandbox                    # sandbox image  (orchestrator-sandbox:local)
```

Python ≥ 3.11. Dependencies (`requirements.txt`): `litellm`, `crewai[a2a]`, `dramatiq[redis]`, `redis`,
`pydantic(-settings)`, `docker`, `ptyprocess`, `httpx`, `rank-bm25`, `croniter`, `skills-ref`, `jsonschema`, `pyyaml`, `python-dotenv`. The `mcp` client library is only
needed to run the MCP supervisor outside the sandbox image (`pip install -e '.[mcp]'`); the sandbox image ships it.

### The full stack

```bash
docker compose up -d --build      # redis · cognee · worker (Dramatiq) · scheduler (dream cycles)
docker compose --profile llm-proxy up -d litellm     # optional OpenAI-compatible LiteLLM proxy

# submit from the host; the workers (in containers, via docker.sock) run the agents in per-project sandboxes
ORCHESTRATOR_EXECUTOR=dramatiq REDIS_URL=redis://localhost:6379/0 COGNEE_URL=http://localhost:8001 \
  orchestrator run "research X and report" --project --role researcher
```

| Service | Role |
|---|---|
| `redis` | Dramatiq broker **and** shared run state (jobs, findings, objectives, guidance, A2A messages) |
| `worker` | `orchestrator worker` — Dramatiq consumers; each job = one agent turn in its project sandbox |
| `scheduler` | `orchestrator scheduler` — enqueues a `dream_cycle` every `DREAMING_INTERVAL_S`, and reaps jobs whose worker died (see below) |
| `cognee` | Cognee memory sidecar (`http://cognee:8001`) — graph recall + the dream/improve pipeline |
| `litellm` *(profile `llm-proxy`)* | OpenAI-compatible LiteLLM proxy for OpenCode / external clients |
| `sandbox` *(profile `build`)* | build target for the sandbox image |

**Worker crashes.** Every running job heartbeats (`JOB_HEARTBEAT_S`). If a worker dies, the scheduler notices the
missing beat after `JOB_STALE_S`, resumes the job (up to `JOB_MAX_ATTEMPTS`, continuing from its last work), and
after that fails it with `worker lost` and tells its parent over A2A.

Workspaces and catalogs are **bind-mounted** (`WORKSPACES_DIR`, `./skills`, `./tools`) so the host CLI and the
workers see the same files. Without Redis the library runs everything in one process (`executor="thread"`).

**Cognee** is a separate server reached over HTTP — not a pip dependency. It is on when `COGNEE_ENABLED=true`
(or `COGNEE_URL`/`COGNEE_HOST` is set); without it memory still works (SQLite + BM25). Cognee's cognify/recall
steps use the LLM configured in `.env` (`OPENROUTER_API_KEY`).

## Quick start

```python
from orchestrator import Orchestrator

orch = Orchestrator()                      # configures from env, seeds the catalog on first use

orch.list_roles()                          # project-manager, reporter, researcher, builder (defaults)

# one role, one CrewAI agent, its bound skills/tools, inside the project sandbox
res = orch.run("write hello.txt in /workspace", role="builder", project_id="demo", remove_sandbox=True)
print(res.ok, res.output)

# the whole crew: manager → specialists (spawn / A2A) → analyzer writes findings/report.md
res = orch.run_project("research X and report", role_ids=["researcher"], project_id="demo2")

# plan → objectives with dependencies → scheduled execution (the objective scheduler)
pid = orch.start_project("research X and report")     # LLM plan, validated (manager first, analyzer last)
orch.project_status(pid)                              # objectives + jobs; dependency standbys are `idle`
orch.wait_project(pid)                                # → completed, or blocked (an operator prompt is open)
orch.reply_to_prompt(pid, prompt_id, "use the fallback source")   # resumes a blocked objective

# durable mode: same API, jobs run on Dramatiq workers, state in Redis
orch = Orchestrator(executor="dramatiq")   # needs `docker compose up -d redis worker` (see above)

# operator console: free text is routed to answer / instruct / replan / stop (or ask one agent via job_id)
orch.chat(pid, "which objectives are blocked?")       # → {"mode": "answer", "reply": ...}
orch.chat(pid, "also cover the staging subnet")       # → replan (operator-named assets are added to the scope rules, if the project has any)
orch.chat(pid, "what did you find so far?", job_id=job_id)    # a direct conversation with one agent

# durable project + schedules (fired by the scheduler service, or `orch.fire_due_schedules()`)
pid = orch.start_project("watch the feed", kind="durable"); orch.accept_plan(pid)
orch.create_schedule(pid, role_id="researcher", brief="check for changes", kind="interval", interval_seconds=3600)

orch.llm_usage_summary(project_id=pid)     # calls / tokens / cost per purpose (agent_loop, planning, dreaming, …)

orch.steer(job_id, "focus on the API")     # live operator guidance (wakes an idle job)
orch.pause(job_id); orch.resume(job_id); orch.kill(job_id)   # operator job control (also *_project)
orch.dream()                               # one dream cycle — you schedule it (the library starts no threads)
```

Operator actions on unknown projects raise `ValueError` (they never create a project); schedules validate the role;
`start_project(objectives=[...])` rejects untitled objectives and unknown roles (before creating anything) and drops
dependency edges that would form a cycle.
`.env` values are loaded at startup and never override real environment variables; `.env.example` lists every option.

CLI / console: `python examples/thin_client.py --help` (`list-roles`, `plan`, `run [--project]`, `dream`,
`suggest-tools`, `draft-skill`, `lint-skill`, `test-tool`, …).

## How it fits together

```text
Orchestrator (api.py)
  └─ LocalControlPlane (host/control.py) ── jobs on a thread pool, A2A hub, run logs, operator hooks
       └─ per job: ProjectSandbox binds the Docker session → run_agent(JobScope(bridge=LocalAgentBridge))
            └─ CrewAIJobRuntime → CrewAI Agent (role + bound skills + crew tools)
                 tools → LocalAgentBridge: spawn/wait · send_message/check_inbox/go_idle · memory · findings
                         objectives · scope rules · workspace artifacts · ask_operator
                 tools → SandboxTools: bash · provision_cli · skill scripts · MCP (inside the sandbox)
  finished runs → JsonRunLog → DreamingService → memory notes (+ Cognee) → staged catalog proposals
  host/: ObjectiveScheduler (deps, standby agents, replan) · OperatorControl + OperatorConsole (instruct/replan/chat)
         GovernanceManager (resident PM) · ScheduleManager · checkpoints · heartbeat/reaper · stream sockets
  engagement/: scope rules (optional), finding normalizer/dedup, asset graph      queue/: Dramatiq actors, Redis state, scheduler loop
```

Behaviours worth knowing:

- **Delegation.** A *dispatch-only* lane specialist (`AGENT_DISPATCH_ONLY`) must delegate: if it finishes without
  spawning a worker it is nudged once, then failed so the scheduler replans. A job that finishes with unread mail
  takes one more turn (bounded, so an agent that ignores its inbox cannot loop). Silent messages are coalesced
  (`A2A_INTERIM_WAKE_UNREAD`); dependency standbys are only ever woken by the scheduler, never by a message or a resume.
- **Plans.** Dependencies that would form a cycle are dropped; a cancelled dependency no longer holds its dependents;
  `start_project(objectives=[...])` rejects untitled objectives and unknown roles before creating anything; a durable
  project whose plan is not accepted yet reports `idle`. If the replan manager itself fails, a `failure_exhausted`
  operator prompt is raised instead of leaving the project silently blocked.
- **Pause / stop / cancel.** Pausing asks running jobs to stop at their next command (resume withdraws a pause that was
  not honored yet); a stopped project stays stopped (`restart_project` is the way back); a job cancelled while running
  still gets its checkpoint, handoff and scheduler bookkeeping from its own worker; a finished project never grows a
  new governance agent. The sandbox is not removed while jobs are still running after a wait timed out.
- **Workspace ownership.** The sandbox runs as root; when a job ends the workspace is chowned back to the host user
  (`SANDBOX_CHOWN`, `SANDBOX_HOST_UID` / `SANDBOX_HOST_GID` — set them for containerised workers), so project folders
  stay editable and deletable without sudo.
- **Recovery.** Jobs heartbeat from the moment they are claimed; a dead worker's job is resumed up to
  `JOB_MAX_ATTEMPTS`. Sibling jobs starting together create the project sandbox once (per-project lock).
- **Scope rules (optional).** A project without a `Roe` is unrestricted; naming a host in chat never creates one. When set, matching is host-aware: exact host or subdomain, CIDR containment for URLs and `host:port` targets,
  URL-path scopes, IPv6 as /128 — never raw substring. Exclusions keep every entry (even `localhost`).
- **Installs.** Pack/skill/marketplace installs skip symlinks, refuse option-like git sources, never read outside the
  pack, and `sync_mcps_from_disk` also prunes catalog rows whose pack folder was deleted.
- **Telemetry.** CrewAI agents use CrewAI's native provider client, so their token/cost usage is recorded per kickoff
  (`llm_usage.record_crew_usage`) rather than through LiteLLM callbacks.

**Replacing a web-app layer.** A web app typically keeps jobs, objectives, findings and messages in a database and runs jobs on
a queue. Here the same contract is `AgentBridgeBase` (what an agent sees) + `run_agent` (execution), implemented
in plain Python by `LocalControlPlane` / `LocalAgentBridge`. A host with its own storage swaps the pieces it owns:

| To replace | Hook |
|---|---|
| Job storage / queue | pass your own `plane=` (subclass `LocalControlPlane`) or implement `AgentBridgeBase` and call `run_agent` yourself |
| Roles / skills / MCP storage | subclass `catalog.PlatformCatalog`, install with `PlatformCatalog.set_shared(...)` |
| Operator prompts (HITL) | `Orchestrator(operator=callable)`; answer later with `reply_to_prompt` |
| Scope rules (optional) | `Roe(in_scope, exclusions, seed)` per project — leave it out for an unrestricted project |
| A2A messages | `A2AHub(store=MessageStore)` |
| Finished runs for dreaming | `DreamingService.shared().run_source = YourRunSource()` |
| Operator settings | `orchestrator.settings.set_resolver(fn)` |
| LLM backend | register a class in `llm.registry`, or point LiteLLM at the proxy |

## Extending (all data, no runtime code)

- **A role** — `PlatformCatalog.shared().upsert_role(role_id=..., kind="technical", goal=..., backstory=...)`.
  Bookend roles (`engagement-pm`, `analyzer`) are protected and unique.
- **A skill** — a directory with `SKILL.md` (+ `scripts/`, `references/`, and a `affinity.yaml` binding it to roles)
  under `skills/`, or `upsert_skill(skill_md=..., bindings=[...])`. Lint: `python examples/thin_client.py lint-skill NAME`.
  Skills without an affinity file are bound to roles by an LLM classifier on first load.
- **A CLI tool** — `tools/catalog/<id>.yaml` (`install:` apt / github_release / git_clone / pip / custom, `verify:`).
  Test it for real: `python examples/thin_client.py test-tool tools/catalog/jq.yaml` (throwaway lab container).
- **An MCP server** — `PlatformCatalog.upsert_mcp({...stdio document...}, bindings=[...])` or a pack under `mcps/`.
  Secrets are never stored in the catalog, only env var names.

## Tests

```bash
pip install -e . pytest
pytest                                   # unit + scripted-CrewAI integration (Docker/Redis tests auto-skip);
                                         # tests/test_regressions.py pins the bugs found in review rounds

REDIS_PORT=6390 docker compose up -d redis && pytest tests/test_queue_redis.py   # real Redis + real Dramatiq worker
ORCH_E2E_STACK=1 pytest tests/test_stack_e2e.py                                  # whole stack in containers (~2 min)
```

`tests/fakellm.py` is a scripted CrewAI LLM, so crews, tools, delegation and A2A are exercised end-to-end without
network access. `tests/test_docker_sandbox.py` runs a real sandbox container and a real learn-lab install.
`tests/test_stack_e2e.py` brings up redis + cognee + worker + scheduler with `docker compose`, points the workers at a
scripted OpenAI-compatible server (`tests/fake_openai_server.py`, in a container), and checks: a job runs in a sandbox
the *worker container* started, a researcher delegates to a builder and is woken by the A2A handoff, memory lands in
Cognee, and the scheduler's dream cycle runs on the worker. (Cognee's own cognify/recall steps need a real API key.)

With a real key in `.env` the same flows were also checked live (single agent in a sandbox, multi-objective project,
LLM planning, console routing, dreaming, Cognee remember → recall, Dramatiq worker + scheduler, and a worker killed
mid-job being reaped and resumed). When testing against workers, make sure no stale `dramatiq` worker from an
earlier run is still consuming jobs with old code. Not exercised live: MCP servers, OpenShell, the marketplace.

## Notes

- Known limits: `CREW_REASONING_*` are inert until a role enables reasoning; the OpenShell runtime has not been run live.
- Skill packs declare role affinity in an `affinity.yaml` next to `SKILL.md`; the first-party skill provider id is
  `native` and the platform-wide memory dataset is `platform`. Older databases are migrated on open; renamed settings are `PLATFORM_MEMORY_ENABLED`, `DREAMING_ENABLED`, `DREAMING_INTERVAL_S`,
  `MCP_STARTUP_SECONDS`, `MCP_SPILL_CHARS`, `MCP_PREVIEW_CHARS` and `ORCHESTRATOR_FINDINGS_QUEUE`.
- Platform skills and prompts (project-manager, analyzer, planners) use generic project vocabulary: objectives,
  findings, report. Domain skills are not included; add your own under `skills/`.
