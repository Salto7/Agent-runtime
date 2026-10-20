"""Finding normalize / record / workspace-queue ingest — the library port of Peon's ``findings.py``.

A finding is an *engagement discovery about subjects* (hosts, services, URLs, files, …) with evidence — never
job/objective progress. ``FindingNormalizer`` demands at least one subject (explicit ``subjects`` or derived from
host/port/url/…). ``FindingService.record`` stores it (deduped), queues novel subjects as RoE *candidates* (not
authorized until promoted), and syncs the asset graph. ``ingest_workspace_findings`` consumes
``workspace/findings_queue.jsonl`` written by skills.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orchestrator.engagement.target_discovery import SubjectCanonicalizer
from orchestrator.engagement.target_shapes import coerce_targets, sanitize_label
from orchestrator.host.state import finding_key

if TYPE_CHECKING:
    from orchestrator.host.control import LocalControlPlane

logger = logging.getLogger(__name__)

# Ranking scale for reports/UI — not a domain taxonomy.
_SEVERITY_RANK = frozenset({"critical", "high", "medium", "low", "info"})
SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")


class FindingNormalizer:
    """Coerce agent/queue dicts into Finding field values."""

    def normalize(self, record: dict[str, Any]) -> dict[str, Any] | None:
        title = str(record.get("title") or "").strip()
        if not title:
            return None
        kind = sanitize_label(
            str(record.get("kind") or ""),
            default="observation",
        )
        asset = sanitize_label(str(record.get("asset_type") or ""), default="")
        severity = sanitize_label(
            str(record.get("severity") or ""),
            default="info",
            max_len=20,
        )
        if severity not in _SEVERITY_RANK:
            severity = "info"
        port = record.get("port")
        try:
            port_i = int(port) if port is not None and str(port).strip() != "" else None
        except (TypeError, ValueError):
            port_i = None
        mitre = record.get("mitre") or record.get("mitre_techniques") or []
        if not isinstance(mitre, list):
            mitre = []
        meta = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        host = str(record.get("host") or "").strip()[:255]
        base = {
            "title": title[:255],
            "kind": kind,
            "asset_type": asset,
            "severity": severity,
            "host": host,
            "service": str(record.get("service") or "").strip()[:128],
            "port": port_i,
            "protocol": str(record.get("protocol") or "").strip()[:16],
            "cve_id": str(record.get("cve_id") or "").strip()[:64],
            "cwe_id": str(record.get("cwe_id") or "").strip()[:32],
            "mitre_techniques": [str(x).strip() for x in mitre if str(x).strip()][:20],
            "description": str(record.get("description") or "").strip()[:8000],
            "evidence": str(record.get("evidence") or "").strip()[:16000],
            "evidence_path": str(record.get("evidence_path") or "").strip()[:1024],
            "remediation": str(record.get("remediation") or "").strip()[:4000],
            "metadata": meta,
        }
        # Prefer explicit subjects; else derive from host/port/url/… — never evidence_path.
        subjects = SubjectCanonicalizer.from_finding(
            {**{k: record[k] for k in ("url", "uri", "repo", "hash", "sample", "malware", "package") if record.get(k)},
             **base, "subjects": record.get("subjects")}
        )
        if not subjects:
            return None
        base["subjects"] = subjects
        return base



class FindingService:
    """Record findings for a project through the control plane's ``StateStore``."""

    def __init__(self, plane: "LocalControlPlane", normalizer: FindingNormalizer | None = None) -> None:
        self.plane = plane
        self.store = plane.store
        self._normalizer = normalizer or FindingNormalizer()

    def record(self, project_id: str, payload: dict[str, Any], *, job_id: str = "", role_id: str = "",
               objective_seq: int = 0) -> dict[str, Any] | None:
        """Normalize + store one finding (deduped). Returns the stored (or already-existing) row, or None if invalid."""
        row = self._normalizer.normalize(payload)
        if row is None:
            return None
        row.update(job_id=job_id, role_id=role_id, objective_seq=objective_seq, status="new",
                   created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        stored = self.store.add_finding(project_id, row)
        if stored is None:  # duplicate: hand back the existing row, change nothing else
            key = finding_key(row)
            return next((f for f in self.store.get_findings(project_id) if finding_key(f) == key), None)
        self._queue_discovered_candidates(project_id, row)
        self.sync_graph(project_id, [stored])
        self.plane.save_findings(project_id)
        return stored

    def sync_graph(self, project_id: str, findings: list[dict[str, Any]]) -> None:
        """Findings are graph producers: persist their subjects at write time (never FIND-n nodes)."""
        try:
            from orchestrator.engagement.asset_graph import AssetGraphEngine
            from orchestrator.engagement.asset_graph import Project as GraphProject

            meta = self.store.get_project(project_id)
            with self.store.lock(f"graph:{project_id}"):
                AssetGraphEngine(GraphProject(project_id, self.store, roe=meta.roe)).sync(findings=findings)
        except Exception as exc:  # noqa: BLE001 — the graph never blocks recording a finding
            logger.warning("asset graph sync failed: %s", exc)

    def _queue_discovered_candidates(self, project_id: str, row: dict[str, Any]) -> None:
        """Novel subjects from structured fields become RoE candidates (surfaced, not authorized)."""
        meta = self.store.get_project(project_id)
        roe = meta.roe
        if roe is None:
            return
        found = SubjectCanonicalizer.from_finding(row)
        known = {t["value"].lower() for t in (coerce_targets(roe.seed) + coerce_targets(roe.in_scope)
                                              + coerce_targets(roe.exclusions) + coerce_targets(roe.candidates))
                 if t.get("value")}
        novel = [t for t in found if t.get("value", "").lower() not in known]
        if novel:
            from orchestrator.engagement.roe_ops import add_candidates

            add_candidates(self.plane, project_id, novel)

    def ingest_workspace_queue(self, project_id: str, workspace: Path, *, job_id: str = "", role_id: str = "",
                               objective_seq: int = 0) -> int:
        """Consume ``workspace/findings_queue.jsonl`` into findings (archived to ``.ingested``)."""
        path = workspace / FINDING_QUEUE
        if not path.is_file():
            return 0
        # Take the file by renaming it: a writer appending right now (an MCP server) keeps its open handle on the
        # renamed file at worst, and a fresh queue file starts for the next row — nothing is truncated away.
        taken = path.with_suffix(path.suffix + ".processing")
        try:
            path.replace(taken)
            path.touch()   # keep an (empty) queue in place for the next writer
            text = taken.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 0
        if not text.strip():
            taken.unlink(missing_ok=True)
            return 0
        n = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict) or not _queue_row_valid(raw):
                continue
            if self.record(project_id, raw, job_id=job_id, role_id=role_id, objective_seq=objective_seq):
                n += 1
        archive = path.with_suffix(path.suffix + ".ingested")
        try:
            prior = archive.read_text(encoding="utf-8", errors="replace") if archive.is_file() else ""
            archive.write_text(prior + text, encoding="utf-8")
            taken.unlink(missing_ok=True)
        except OSError:
            pass
        return n


FINDING_QUEUE = Path("workspace") / "findings_queue.jsonl"
_FINDING_QUEUE_SCHEMA_PATH = Path(__file__).resolve().parent / "finding_queue.schema.json"
_FINDING_QUEUE_SCHEMA: dict[str, Any] | None = None


def _finding_queue_schema() -> dict[str, Any] | None:
    global _FINDING_QUEUE_SCHEMA
    if _FINDING_QUEUE_SCHEMA is not None:
        return _FINDING_QUEUE_SCHEMA or None
    try:
        _FINDING_QUEUE_SCHEMA = json.loads(_FINDING_QUEUE_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _FINDING_QUEUE_SCHEMA = {}
    return _FINDING_QUEUE_SCHEMA or None


def _queue_row_valid(raw: dict[str, Any]) -> bool:
    """Validate a queue row against the shared JSON Schema (when jsonschema + the schema are available)."""
    if not str(raw.get("title") or "").strip():
        return False
    try:
        import jsonschema
    except ImportError:
        return True
    schema = _finding_queue_schema()
    if not schema:
        return True
    try:
        jsonschema.validate(raw, schema)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("findings_queue row rejected: %s", exc)
        return False
