---
name: analyzer
description: Post-stop synthesizer — aggregate the project plan, workspace evidence, and curated findings into a standalone penetration-test report. No new evidence collection. Use after project stop to synthesize findings/report.md.
allowed-tools: Bash Read
---

## Goal
After the project stops, write a **standalone** `findings/report.md` from existing
evidence only. **Do not collect new evidence or invent discoveries.**

## Order
Runs **last** on every project (planner bookend): `project-manager` → specialist
skills → **`analyzer`** (this skill) → `findings/report.md`.

## Sources (read-only)
1. Structured findings (`list_findings` / DB / `workspace/findings_queue.jsonl`)
2. Objective statuses + scope rules
3. Workspace JSON handoffs under `workspace/` and `workspace/mcp/artifacts/`
   (pipeline reports, plugin dumps). Prefer the largest complete JSON for a
   given run over truncated previews.
4. Curated `findings/*.md` — supporting detail only

See [report format](references/REPORT.md) for required sections and hard rules.

## Required workflow
1. `list_workspace_artifacts` — note paths **and byte sizes**.
2. `list_findings` — board rows already curated by specialists.
3. For each substantial JSON artifact, `read_workspace_artifact` with
   `max_chars` ≥ file size (default tool limit is not enough for large MCP
   dumps). If the response says truncated, re-read higher.
   If a shell read fails, fall back to `read_workspace_artifact` — **never
   write the report from handoff summaries alone**.
4. From JSON arrays (`dangling_domains`, `domains`, entities, …): **enumerate
   every row** into the report; `record_findings` for security-relevant rows
   that are not already on the board. Do not stop at an aggregate row such as
   "N items flagged" — one finding per item, or a table row for each. If an
   expected collection is empty, say so and quote the recorded `errors` /
   `notes` (e.g. a source that failed to run) so the gap is attributed, not
   silently omitted.
5. Write `findings/report.md` as a **penetration-test / red-team report**
   (via `write_report_note` sections or a single full write), with these
   top-level sections in order: **Executive summary**; **Scope and
   methodology**; **Findings summary** (ID/severity/asset table);
   **Detailed findings** (one `F-nn` block each: description, evidence,
   impact, remediation); **Recommendations** (prioritized); **Appendix A.
   Steps taken and commands executed**; **Appendix B. Limitations**. Embed
   inventories and findings inline — never "see file X". See
   [report format](references/REPORT.md).

## Run
Start with the deterministic builder — it parses every workspace JSON artifact
(MCP envelopes unwrapped), lists **every** domain / entity row, attributes tool
errors, and writes the full report skeleton:

```
run_skill_script("analyzer", "scripts/run.sh", args='[]')
```

Then add judgement on top with `write_report_note(section, body)`. A note whose
heading matches an existing section **replaces** it (use the draft's exact
headings, e.g. `1. Executive summary`, `4. Detailed findings`) — never append a
"corrected" copy. `Appendix C. Asset inventory` is generated
from the evidence and is read-only (writing to it is refused) — reference it
from your findings instead of re-typing or abbreviating the lists. If a hand-written `findings/report.md` already exists the
builder writes `findings/report.offline.md` instead of overwriting it.
