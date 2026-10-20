"""Build findings/report.md for offline / non-worker analyzer runs.

Layout (see ../references/REPORT.md) — a penetration-test / red-team report:

1. Executive summary
2. Scope and methodology
3. Findings summary
4. Detailed findings
5. Recommendations
Appendix A. Steps taken and commands executed
Appendix B. Limitations

Keep this skill-side path free of orchestrator imports.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

_SEV_ORDER = ["critical", "high", "medium", "low", "info"]
_SEV_LABEL = {
    "critical": "Critical",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "info": "Informational",
}
_OUTPUT_CAP = 1500
_DETAIL_CAP = 4000


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _section(text: str, name: str) -> str:
    m = re.search(rf"^## {re.escape(name)}\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1) if m else ""


def _clip(text: str, cap: int = _OUTPUT_CAP) -> str:
    text = text.strip()
    return text if len(text) <= cap else text[:cap].rstrip() + "\n… (truncated)"


def _cell(value: object) -> str:
    """Make a value safe for a single markdown table cell."""
    text = " ".join(str(value or "").split())
    return text.replace("|", "\\|") or "—"


def _first(row: dict, *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, "", [], {}):
            return str(value).strip()
    return ""


def _runs(findings_dir: Path) -> list[dict]:
    """Parse per-skill ``findings/<skill>.md`` run notes into structured rows."""
    runs: list[dict] = []
    if not findings_dir.is_dir():
        return runs
    for path in sorted(findings_dir.glob("*.md")):
        if path.name.lower() == "report.md":
            continue
        text = _read(path).strip()
        if not text:
            continue
        cmds = re.findall(r"^- `(.+)`\s*$", _section(text, "Commands"), re.M)
        exit_m = re.search(r"^Exit:\s*`?(-?\d+)`?", text, re.M)
        bin_m = re.search(r"^Binary:\s*`?([^`\n]+)`?", text, re.M)
        out = _section(text, "Output")
        out = re.sub(r"^```[a-zA-Z]*\n?|\n?```\s*$", "", out.strip())
        runs.append(
            {
                "skill": path.stem,
                "binary": bin_m.group(1).strip() if bin_m else "",
                "exit": int(exit_m.group(1)) if exit_m else None,
                "commands": cmds,
                "output": out,
                "body": text,
            }
        )
    return runs


def _queue(ws: Path) -> list[dict]:
    rows: list[dict] = []
    for line in _read(ws / "workspace" / "findings_queue.jsonl").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


_SKIP_ARTIFACTS = {"findings_queue.jsonl", "analysis_context.json"}


def _unwrap(obj: object) -> object:
    """Unwrap an MCP tool-result envelope: ``{"content":[{"text":"<json>"}]}``."""
    if isinstance(obj, dict) and isinstance(obj.get("content"), list):
        for part in obj["content"]:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                try:
                    return json.loads(part["text"])
                except ValueError:
                    continue
    return obj


def _artifacts(ws: Path) -> list[tuple[str, object]]:
    """Every parseable workspace JSON artifact (MCP envelopes unwrapped)."""
    out: list[tuple[str, object]] = []
    base = ws / "workspace"
    if not base.is_dir():
        return out
    for path in sorted(base.rglob("*.json")):
        if path.name in _SKIP_ARTIFACTS:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        out.append((str(path.relative_to(ws)), _unwrap(data)))
    return out


def _walk(obj: object, key: str = ""):
    """Yield ``(key, value)`` for every dict entry in a nested structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v, k)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item, key)


# Keys whose lists are tool chatter / provenance, not engagement inventory.
_NOISE_KEYS = {
    "errors", "error", "notes", "warnings", "queries", "sources", "content",
    "tags", "logs", "trace", "headers",
}
_MAX_COLS = 6
_MAX_ROWS = 1000
_CELL_CAP = 160


def _inventories(
    artifacts: list[tuple[str, object]],
) -> tuple[dict[str, tuple[list, str]], list[str]]:
    """Collections found in the evidence, independent of any tool's schema.

    Every non-empty list of objects/scalars, keyed by the field name that holds
    it. The same collection often appears twice (a summary view and the full
    list), so the largest one per key wins. Also returns keys whose list was
    present but **empty**, so a discovery step that returned nothing is visible.
    """
    best: dict[str, tuple[list, str]] = {}
    empty: list[str] = []
    for path, data in artifacts:
        for key, value in _walk(data):
            if key in _NOISE_KEYS or not isinstance(value, list):
                continue
            if not value:
                if key not in empty:
                    empty.append(key)
                continue
            if not all(isinstance(v, (dict, str, int, float)) for v in value):
                continue
            if len(value) > len(best.get(key, ([], ""))[0]):
                best[key] = (value, path)
    empty = [k for k in empty if k not in best]
    return best, empty


def _label(key: str) -> str:
    return key.replace("_", " ").strip().capitalize()


def _columns(rows: list) -> list[str]:
    counts: dict[str, int] = {}
    for row in rows:
        if isinstance(row, dict):
            for k, v in row.items():
                if isinstance(v, (str, int, float, bool)) and v not in ("", None):
                    counts[k] = counts.get(k, 0) + 1
    # Stable: ties keep the tool's own field order (identifier fields lead).
    ranked = sorted(counts, key=lambda k: -counts[k])
    return ranked[:_MAX_COLS]


def _short(value: object) -> str:
    text = _cell(value)
    return text if len(text) <= _CELL_CAP else text[: _CELL_CAP - 1] + "…"


def _inventory_sections(
    inv: dict[str, tuple[list, str]], empty: list[str]
) -> list[str]:
    """Markdown blocks listing EVERY row (never a bare count)."""
    blocks: list[str] = []
    for key, (rows, path) in sorted(inv.items(), key=lambda kv: -len(kv[1][0])):
        shown = rows[:_MAX_ROWS]
        cols = _columns(shown)
        if cols:
            head = "| " + " | ".join(cols) + " |"
            sep = "| " + " | ".join("---" for _ in cols) + " |"
            body = [
                "| " + " | ".join(_short(r.get(c)) if isinstance(r, dict) else "—" for c in cols) + " |"
                for r in shown
            ]
        else:  # scalar items
            head, sep = f"| {key} |", "| --- |"
            body = [f"| {_short(r)} |" for r in shown]
        more = (
            f"\n\n_{len(rows) - len(shown)} more row(s) in `{path}`._"
            if len(rows) > len(shown)
            else ""
        )
        blocks.append(
            f"### {_label(key)} ({len(rows)})\n\n_Source: `{path}`_\n\n"
            + "\n".join([head, sep, *body])
            + more
        )
    if empty:
        blocks.append(
            "### Empty result sets\n\n"
            "These collections were present in the evidence but contained no "
            "items (a source that returned nothing, or did not run — see "
            "Appendix B):\n\n" + "\n".join(f"- {_label(k)}" for k in empty)
        )
    return blocks


def _diagnostics(artifacts: list[tuple[str, object]]) -> list[str]:
    """Tool-recorded ``errors`` / ``notes`` — surfaced so gaps are attributed."""
    seen: list[str] = []
    for _path, data in artifacts:
        for key, value in _walk(data):
            if key in {"errors", "notes", "error"}:
                items = value if isinstance(value, list) else [value]
                for item in items:
                    text = " ".join(str(item or "").split())
                    if text and text.lower() != "none" and text[:200] not in seen:
                        seen.append(text[:200])
    return seen


def _sev(row: dict) -> str:
    s = str(row.get("severity") or "info").lower()
    return s if s in _SEV_ORDER else "info"


def _title(row: dict) -> str:
    return _first(row, "title", "name", "kind") or "Untitled finding"


def _asset(row: dict) -> str:
    asset = _first(row, "asset", "target", "host")
    port = _first(row, "port")
    service = _first(row, "service")
    if asset and port:
        asset = f"{asset}:{port}"
    if service:
        asset = f"{asset} ({service})" if asset else service
    if not asset:
        subjects = row.get("subjects")
        if isinstance(subjects, list):
            vals = [
                str(s.get("value"))
                for s in subjects
                if isinstance(s, dict) and s.get("value")
            ]
            asset = ", ".join(vals)
    return asset


def _refs(row: dict) -> list[str]:
    refs: list[str] = []
    for key, label in (("cve_id", "CVE"), ("cwe_id", "CWE")):
        value = _first(row, key)
        if value:
            refs.append(f"{label}: {value}")
    mitre = row.get("mitre_techniques")
    if isinstance(mitre, list) and mitre:
        refs.append("MITRE ATT&CK: " + ", ".join(str(m) for m in mitre))
    return refs


def _counts(rows: list[dict]) -> dict[str, int]:
    by_sev = dict.fromkeys(_SEV_ORDER, 0)
    for row in rows:
        by_sev[_sev(row)] += 1
    return by_sev


def _risk_rating(by_sev: dict[str, int]) -> str:
    for sev in ("critical", "high", "medium", "low"):
        if by_sev.get(sev):
            return _SEV_LABEL[sev]
    return "Informational" if by_sev.get("info") else "Not determined"


def _fid(index: int) -> str:
    return f"F-{index:02d}"


def _executive_summary(
    runs: list[dict], rows: list[dict], inv: dict[str, tuple[list, str]] | None = None
) -> str:
    by_sev = _counts(rows)
    n_fail = sum(1 for r in runs if r.get("exit") not in (None, 0))
    total = len(rows)
    lines = [
        f"**Overall risk rating:** {_risk_rating(by_sev)}",
        "",
        f"This assessment executed {len(runs)} tool run(s)"
        + (f" ({n_fail} exited non-zero)" if n_fail else "")
        + f" and recorded {total} finding(s).",
        "",
        "| Severity | Count |",
        "| --- | ---: |",
    ]
    for sev in _SEV_ORDER:
        lines.append(f"| {_SEV_LABEL[sev]} | {by_sev[sev]} |")
    inv = inv or {}
    facts = [
        f"{len(rows_)} {_label(key).lower()}"
        for key, (rows_, _path) in list(inv.items())[:6]
    ]
    if facts:
        lines += ["", "**Evidence inventory:** " + ", ".join(facts) + " (see Appendix C)."]
    top = [r for r in rows if _sev(r) in {"critical", "high"}][:5]
    if top:
        lines += ["", "**Key issues requiring attention:**", ""]
        for idx, row in enumerate(rows, 1):
            if row in top:
                lines.append(
                    f"- {_fid(idx)} — {_title(row)} ({_SEV_LABEL[_sev(row)]})"
                )
    elif not total:
        lines += ["", "_No findings were recorded for this engagement._"]
    return "\n".join(lines)


def _scope_and_method(runs: list[dict]) -> str:
    tools = sorted({r["binary"] or r["skill"] for r in runs})
    lines = [
        "**Scope and scope rules:** as authorized for this engagement; "
        "all activity was limited to in-scope targets. Out-of-scope assets were "
        "not tested.",
        "",
        "**Approach:** reconnaissance and enumeration, followed by validation of "
        "exposed services and weaknesses, then synthesis of the evidence below. "
        "No new evidence was collected during reporting.",
    ]
    if tools:
        lines += ["", "**Tooling used:** " + ", ".join(f"`{t}`" for t in tools)]
    return "\n".join(lines)


def _summary_table(rows: list[dict]) -> str:
    if not rows:
        return "_No findings recorded._"
    out = [
        "| ID | Severity | Finding | Affected asset |",
        "| --- | --- | --- | --- |",
    ]
    for idx, row in enumerate(rows, 1):
        out.append(
            f"| {_fid(idx)} | {_SEV_LABEL[_sev(row)]} | {_cell(_title(row))} "
            f"| {_cell(_asset(row))} |"
        )
    return "\n".join(out)


def _finding_detail(idx: int, row: dict) -> str:
    sev = _SEV_LABEL[_sev(row)]
    lines = [f"### {_fid(idx)} — {_title(row)}", ""]
    meta = [f"**Severity:** {sev}"]
    asset = _asset(row)
    if asset:
        meta.append(f"**Affected asset:** `{asset}`")
    status = _first(row, "status")
    if status:
        meta.append(f"**Status:** {status}")
    refs = _refs(row)
    lines.append("  \n".join(meta))
    if refs:
        lines += ["", "**References:** " + "; ".join(refs)]
    description = _first(row, "description", "detail")
    if description:
        lines += ["", "**Description**", "", _clip(description, _DETAIL_CAP)]
    evidence = _first(row, "evidence")
    if evidence:
        lines += ["", "**Evidence**", "", "```", _clip(evidence, _DETAIL_CAP), "```"]
    impact = _first(row, "impact")
    if impact:
        lines += ["", "**Impact**", "", impact]
    remediation = _first(row, "remediation", "recommendation")
    if remediation:
        lines += ["", "**Remediation**", "", remediation]
    return "\n".join(lines)


def _recommendations(runs: list[dict], rows: list[dict]) -> str:
    buckets = {
        "Immediate (critical / high)": ("critical", "high"),
        "Short term (medium)": ("medium",),
        "Long term / hardening (low / informational)": ("low", "info"),
    }
    out: list[str] = []
    for label, sevs in buckets.items():
        items = []
        for idx, row in enumerate(rows, 1):
            rec = _first(row, "remediation", "recommendation")
            if rec and _sev(row) in sevs:
                items.append(f"- **{_fid(idx)} {_title(row)}:** {rec}")
        if items:
            out += [f"### {label}", "", *items, ""]
    gaps = [
        f"- Re-run `{r['skill']}` (`{r['binary'] or 'tool'}`) — it exited "
        f"non-zero (`{r['exit']}`); its output should not be relied on."
        for r in runs
        if r.get("exit") not in (None, 0)
    ]
    if gaps:
        out += ["### Coverage gaps / follow-up", "", *gaps, ""]
    if not out:
        out = [
            "_No remediation items were attached to findings; re-run under the "
            "orchestrator worker analyzer for structured recommendations._"
        ]
    return "\n".join(out).rstrip()


def _steps(runs: list[dict]) -> str:
    steps: list[str] = []
    for run in runs:
        head = f"### `{run['skill']}`"
        if run["binary"]:
            head += f" — `{run['binary']}`"
        if run["exit"] is not None:
            head += f" (exit {run['exit']})"
        cmd_block = (
            "\n".join(f"- `{c}`" for c in run["commands"])
            if run["commands"]
            else "_No commands recorded._"
        )
        out_block = _clip(run["output"]) or "_No output captured._"
        steps.append(
            f"{head}\n\nCommands:\n{cmd_block}\n\nOutput excerpt:\n```\n{out_block}\n```"
        )
    return "\n\n".join(steps) or "_No tool runs recorded._"


def build_report(ws: Path) -> Path:
    findings_dir = ws / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)
    report = findings_dir / "report.md"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    runs = _runs(findings_dir)
    rows = _queue(ws)
    rows.sort(key=lambda r: _SEV_ORDER.index(_sev(r)))
    artifacts = _artifacts(ws)
    inventories, empty_sets = _inventories(artifacts)
    diagnostics = _diagnostics(artifacts)

    details = (
        "\n\n".join(_finding_detail(i, r) for i, r in enumerate(rows, 1))
        if rows
        else "_No structured findings were recorded for this engagement._"
    )

    inv_blocks = _inventory_sections(inventories, empty_sets)
    appendix_c = ""
    if inv_blocks:
        details = (
            details
            + "\n\n_Complete per-row inventories (every domain, entity and "
            "flagged asset) are in Appendix C._"
        )
        appendix_c = "\n".join(
            [
                "## Appendix C. Asset inventory",
                "<!-- orchestrator:generated -->",
                "",
                "Generated from the engagement artifacts — one row per item.",
                "",
                "\n\n".join(inv_blocks),
                "",
            ]
        )
    limitations = (
        "This report reflects only evidence present in the engagement "
        "workspace at generation time. Absence of a finding is not proof "
        "of absence of a weakness."
    )
    if diagnostics:
        limitations += (
            "\n\nTool-recorded errors / notes (a failed source means that "
            "discovery step did not run, not that it found nothing):\n\n"
            + "\n".join(f"- {d}" for d in diagnostics[:20])
        )

    body = "\n".join(
        [
            "# Penetration Test Report",
            "",
            f"**Date:** {stamp}  ",
            "**Classification:** Confidential  ",
            "**Prepared by:** analyzer skill (offline)",
            "",
            "## 1. Executive summary",
            "",
            _executive_summary(runs, rows, inventories),
            "",
            "## 2. Scope and methodology",
            "",
            _scope_and_method(runs),
            "",
            "## 3. Findings summary",
            "",
            _summary_table(rows),
            "",
            "## 4. Detailed findings",
            "",
            details,
            "",
            "## 5. Recommendations",
            "",
            _recommendations(runs, rows),
            "",
            "## Appendix A. Steps taken and commands executed",
            "",
            _steps(runs),
            "",
            "## Appendix B. Limitations",
            "",
            limitations,
            "",
            appendix_c,
        ]
    )
    marker = "**Prepared by:** analyzer skill (offline)"
    if report.is_file():
        current = _read(report)
        # A report written by agents/operators (no generator marker) is the
        # source of truth — never replace it with this deterministic draft.
        if current.strip() and marker not in current and "analyzer skill (offline)" not in current:
            report = findings_dir / "report.offline.md"
    report.write_text(body, encoding="utf-8")
    return report
