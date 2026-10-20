# project-manager execution reference

## Paths
- Scratch → `workspace/`
- Phase outputs → `findings/<skill-or-phase>.md` (+ structured JSON under `workspace/`)
- Final `findings/report.md` → **analyzer**

## Who does what
- **Engagement PM (this role)** — govern objectives, replan / ask operator, never probe
- **Technical experts** — catalog roles (network, OSINT, web, SQLi, …); own a skill
  group; spawn skill workers for parallel work
- **Skill workers** — child Jobs under an expert; focused brief + one or more skills
- **Analyzer** — report only after specialists complete

## Sequencing
- Honor project-plan `depends_on` (scheduler-enforced)
- Fan-out: `spawn_work(..., link="child")` → `wait_for_work`
- Hand off via `workspace/` artifacts the next skill documents — not by parsing
  `findings/*.md`

## Rules
1. CLIs come from **`tools/catalog`** via `provision_cli` / worker provision.
2. Prefer expert skill packs over ad-hoc one-off shells.
3. Never invent subjects or results when a skill or tool exists.
4. `record_finding` is for engagement discoveries about subjects with evidence —
   not job/objective/agent status.
5. On conflict / block: replan if cheap; otherwise `ask_operator`. Operator
   guidance is learned experience for later runs.
