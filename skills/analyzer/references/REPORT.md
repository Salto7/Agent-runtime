# analyzer report format

## Report (`findings/report.md`) — penetration-test / red-team report
Must be readable alone by a client: an executive audience first, then the
technical detail. Copy data into the body (no "see file X"). Use these
top-level sections, in this order, and no others:

1. **Executive summary** — non-technical. Engagement purpose, **overall risk
   rating**, a findings-by-severity table, and the handful of issues that need
   attention first. No command output here.
2. **Scope and methodology** — what was in scope (from the RoE), what was
   excluded, the approach/phases and tooling used.
3. **Findings summary** — one table: `ID | Severity | Finding | Affected
   asset`. IDs (`F-01`, `F-02`, …) are stable and reused below. Order by
   severity: critical → high → medium → low → informational.
4. **Detailed findings** — one `###` block per finding, titled `F-nn — title`:
   - Severity, affected asset(s), status
   - References where evidence supports them (CVE, CWE, MITRE ATT&CK)
   - **Description** — what the weakness is
   - **Evidence** — the actual output / request-response / enumerated rows
   - **Impact** — what an attacker gains (only what the evidence supports)
   - **Remediation** — concrete, actionable fix
   Include inventories (hosts, URLs, services, identities, …) inline here;
   columns follow the evidence, do not force a fixed schema.
5. **Recommendations** — consolidated and prioritized: Immediate
   (critical/high), Short term (medium), Long term / hardening (low/info),
   plus coverage gaps and follow-up testing.
6. **Appendix A. Steps taken and commands executed** — every tool run, in
   order, with the exact command line(s) and an evidence-bearing excerpt of
   its output.
7. **Appendix B. Limitations** — what was not tested or could not be
   verified, and any failed/partial tool runs.

For multi-step engagements where findings chain (foothold → pivot → impact),
add an **Attack narrative** subsection under Detailed findings that orders
the findings as a kill chain. Only chain steps the evidence demonstrates.

Mark a reporting objective `completed` if present. Skills queue structured
rows via `record_finding` → `workspace/findings_queue.jsonl` (peon ingests to
Finding). Use DB findings **and** every concrete row in workspace JSON arrays
(for example `dangling_domains`, verified `domains`, entities). Phase markdown
(`findings/<skill>.md`) is the source for Appendix A.

Findings are **engagement discoveries** about any subject class, with
evidence — not skill/job/objective completion status. `kind` / `asset_type`
are free-form.

## Hard rules
- No new evidence collection outside existing workspace artifacts
- No fabricated discoveries, assets, identifiers, CVEs, CVSS scores or impact.
  If severity/impact is not supported by the evidence, say so; do not inflate
- Intermediate phase files may stay on disk; **submit only** `findings/report.md`
- **Never collapse a JSON array into a count.** If evidence has
  `dangling_domains: […]` (or similar), the report must list **every** entry
  (domain, kind, evidence) under "Detailed findings."
  Saying "N indicators — see artifact" is a failed report.
- Before reading large JSON, check the artifact size from
  `list_workspace_artifacts`. Call
  `read_workspace_artifact(path, max_chars=…)` with `max_chars` at least the
  file size. If the tool returns a truncation notice, re-read with a higher
  limit — truncated JSON drops trailing arrays and produces incomplete reports.
- Batch-record structured Finding rows (`record_findings`) for each security-
  relevant observation (each dangling domain, each confirmed exposure) before
  or while writing the report, so the findings board matches the report body.
- Every command actually executed must appear in Appendix A — do not
  summarize a run as "ran nmap" without the exact command line and output
  excerpt.
