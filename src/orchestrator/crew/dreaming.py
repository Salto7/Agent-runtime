"""Background Cognee improve (dream) cycles.

The host decides *when* a cycle runs (this library starts no threads) and
supplies finished runs through a ``RunSource`` (default: ``JsonRunLog``).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestrator import settings
from orchestrator.config import get_config
from orchestrator.crew.memory import MemoryService, job_session_id, project_dataset
from orchestrator.crew.runs import TERMINAL_STATUSES, JsonRunLog, RunSource
from orchestrator.utils.service import SharedServiceBase

logger = logging.getLogger(__name__)

# Platform dataset only: lessons, gists and playbooks. Project datasets are
# engagement transcripts and are not a source of generic improvements.
_SUMMARY_DATASET = "peon"
_SUMMARY_QUERIES = (
    "suggested platform improvements tooling gaps missing tools fixes",
    "lessons learned failures pitfalls what should change",
    "playbooks recurring issues recommended changes",
)
_SUMMARY_BUDGET = 24_000
_SUMMARY_SYSTEM = (
    "You summarize suggested improvements that are already written in the "
    "memory excerpts. Use only those excerpts. Do not invent an improvement "
    "they do not support, and do not add target names, companies, domains, "
    "IP addresses, URLs or email addresses. Write plain text for an operator, "
    "with no markdown, asterisks or headings:\n"
    "1. Two to four sentences on the themes.\n"
    "2. A list. Each item starts with a dash and states one improvement, why "
    "the memory mentions it, and how often if the excerpts say.\n"
    "If the excerpts contain no improvements, say that in one sentence. "
    "Do not apply, edit, schedule or save anything."
)




_MERGE_SYSTEM = (
    "Combine these partial summaries of the same memory into one summary. "
    "Keep every distinct improvement. Merge duplicates and keep the highest "
    "frequency when both mention one. Write plain text with no markdown, "
    "asterisks or headings: a short paragraph, then a dash list. Do not invent "
    "improvements, and do not apply, edit, schedule or save anything."
)






class DreamingService(SharedServiceBase):
    """Select due sessions and call Cognee ``improve`` offline."""

    #: Where finished runs come from. Replace with a host ``RunSource``.
    run_source: RunSource | None = None

    def runs(self) -> RunSource:
        if self.run_source is None:
            self.run_source = JsonRunLog()
        return self.run_source

    def enabled(self) -> bool:
        return settings.get_bool("PEON_DREAMING_ENABLED", True) and MemoryService.enabled()

    def cursor_path(self) -> Path:
        root = Path(get_config().platform_workspace_dir).resolve()
        path = root / "peon" / "dream_cursor.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def load_cursor(self) -> dict[str, Any]:
        path = self.cursor_path()
        if not path.is_file():
            return {"sessions": {}, "last_run_at": ""}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"sessions": {}, "last_run_at": ""}
        return data if isinstance(data, dict) else {"sessions": {}, "last_run_at": ""}

    def save_cursor(self, data: dict[str, Any]) -> None:
        path = self.cursor_path()
        try:
            path.write_text(
                json.dumps(data, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.debug("dream cursor write failed: %s", exc)

    def run_cycle(self, *, limit: int = 20) -> dict[str, Any]:
        """Improve due project sessions; return structured report."""
        if not self.enabled():
            return {"ok": False, "skipped": True, "reason": "disabled"}
        now = datetime.now(UTC).isoformat(timespec="seconds")
        cursor = self.load_cursor()
        sessions = cursor.get("sessions") if isinstance(cursor.get("sessions"), dict) else {}
        due = self._due_sessions(limit=limit)
        improved: list[dict[str, Any]] = []
        errors: list[str] = []
        for item in due:
            dataset = item["dataset"]
            session_id = item["session_id"]
            result = MemoryService.improve_sessions(
                dataset=dataset,
                session_ids=[session_id],
                run_in_background=True,
            )
            row = {
                "dataset": dataset,
                "session_id": session_id,
                "project_id": item.get("project_id") or "",
                "ok": bool(result.get("ok")),
            }
            if result.get("ok"):
                sessions[session_id] = {
                    "dataset": dataset,
                    "project_id": item.get("project_id") or "",
                    "last_improved_at": now,
                    "job_updated_at": item.get("job_updated_at") or "",
                }
            else:
                err = str(result.get("error") or result.get("status_code") or "improve failed")
                row["error"] = err[:300]
                errors.append(f"{session_id}: {err[:200]}")
            improved.append(row)
        # Curate each finished agent's transcript into a gist (and supply the
        # transcript to Cognee). Gists are what planners later recall.
        gists = self.curate_gists(cursor, now=now)
        synthesis = self.synthesize(cursor, now=now)
        proposals: dict[str, Any] | None = None
        if (synthesis or {}).get("saved"):
            # Only worth asking when synthesis actually produced new playbooks
            # or a platform-improvements note to propose changes from.
            proposals = self.propose_catalog_changes()
        if any(g.get("gist") for g in gists) or (synthesis or {}).get("saved"):
            self.sync_platform_index()
        # Consolidate the platform dataset only when it actually gained
        # something; an unconditional pass re-distils whatever Q&A is cached.
        peon_result: dict[str, Any] = {"ok": True, "skipped": True, "reason": "no new gists"}
        if any(g.get("gist") for g in gists) or (synthesis or {}).get("saved"):
            peon_result = MemoryService.improve_sessions(
                dataset="peon",
                session_ids=None,
                run_in_background=True,
            )
        cursor["sessions"] = sessions
        cursor["last_run_at"] = now
        cursor["last_report"] = {
            "at": now,
            "improved": improved,
            "gists": gists,
            "synthesis": synthesis,
            "proposals": proposals,
            "peon_ok": bool(peon_result.get("ok")),
            "errors": errors[:20],
        }
        self.save_cursor(cursor)
        return {
            "ok": True,
            "at": now,
            "count": len(improved),
            "improved": improved,
            "gists": gists,
            "synthesis": synthesis,
            "proposals": proposals,
            "peon": peon_result,
            "errors": errors,
        }

    def clear(self, *, reindex: bool = True) -> dict[str, Any]:
        """Wipe Cognee, reset what the dream cycle tracked about it, optionally re-index.

        Peon's own records (memory notes, gists, playbooks) are untouched; they
        are the source of truth. With ``reindex`` the platform dataset is rebuilt
        from them straight away, otherwise the next dream cycle that writes a
        gist or playbook rebuilds it.
        """
        if not self.enabled():
            return {"ok": False, "skipped": True, "reason": "disabled"}
        from orchestrator.crew.cognee_backend import CogneeBackend

        wiped = CogneeBackend.shared().clear_all()
        cursor = self.load_cursor()
        cursor["sessions"] = {}  # the sessions they referred to are gone
        cursor.pop("last_report", None)
        self.save_cursor(cursor)
        index = self.sync_platform_index() if reindex and wiped.get("ok") else None
        return {"ok": bool(wiped.get("ok")), **wiped, "reindex": index}

    def sync_platform_index(self) -> dict[str, Any]:
        """Keep the Cognee retrieval index equal to the platform memory records."""
        try:
            from orchestrator.memory.store import sync_platform_dataset

            return sync_platform_dataset()
        except Exception as exc:  # noqa: BLE001
            logger.warning("platform index sync failed: %s", exc, exc_info=True)
            return {"ok": False}

    def synthesize(self, cursor: dict[str, Any], *, now: str) -> dict[str, Any] | None:
        """Overall lessons learned across runs (playbooks + platform improvements)."""
        try:
            from orchestrator.crew.gist import maybe_synthesize

            return maybe_synthesize(cursor, now=now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("playbook synthesis failed: %s", exc, exc_info=True)
            return None

    def propose_catalog_changes(self) -> dict[str, Any] | None:
        """Ask the LLM to suggest role/skill catalog changes, staged for review."""
        try:
            from orchestrator.crew.gist import propose_catalog_changes

            return propose_catalog_changes()
        except Exception as exc:  # noqa: BLE001
            logger.warning("catalog proposal generation failed: %s", exc, exc_info=True)
            return None

    def curate_gists(
        self, cursor: dict[str, Any], *, now: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Supply + curate finished agents not yet processed (bounded LLM calls)."""
        from orchestrator.crew.gist import process_job

        done = cursor.setdefault("gists", {})
        out: list[dict[str, Any]] = []

        def _llm_calls() -> int:
            # Trivial runs are skipped without an LLM call and don't count.
            return sum(1 for r in out if not r.get("skipped"))

        try:
            for run in self.runs().finished_runs(limit=60):
                if _llm_calls() >= limit:
                    break
                key = str(run.job_id)
                if run.status not in TERMINAL_STATUSES or key in done:
                    continue
                try:
                    row = process_job(run)
                except Exception as exc:  # noqa: BLE001 — one bad run must not stall curation behind it
                    logger.warning("gist for run %s failed: %s", key, exc)
                    done[key] = {"at": now, "scope": "", "skipped": f"error: {str(exc)[:120]}"}
                    continue
                if row.get("retry"):
                    out.append(row)  # LLM unavailable: try again next cycle
                    continue
                done[key] = {
                    "at": now,
                    "scope": row.get("scope", ""),
                    "skipped": row.get("skipped", ""),
                }
                out.append(row)
        except Exception as exc:  # noqa: BLE001
            logger.warning("gist curation failed: %s", exc, exc_info=True)
        return out

    def status(self) -> dict[str, Any]:
        cursor = self.load_cursor()
        health = MemoryService.health()
        return {
            "enabled": self.enabled(),
            "cognee": health,
            "last_run_at": cursor.get("last_run_at") or "",
            "last_report": cursor.get("last_report") or {},
            "session_count": len(cursor.get("sessions") or {}),
        }

    def _due_sessions(self, *, limit: int = 20) -> list[dict[str, str]]:
        """Recent run sessions whose run advanced since they were last improved."""
        try:
            runs = list(self.runs().finished_runs(limit=max(1, min(80, limit * 3))))
        except Exception as exc:  # noqa: BLE001
            logger.debug("dream due sessions query failed: %s", exc)
            return []
        cursor = self.load_cursor()
        seen = cursor.get("sessions") if isinstance(cursor.get("sessions"), dict) else {}
        eligible = TERMINAL_STATUSES | {"running"}
        out: list[dict[str, str]] = []
        claimed: set[str] = set()
        for run in runs:
            if not run.project_id or run.status not in eligible:
                continue
            sid = job_session_id(run)
            if not sid or sid in claimed:
                continue
            updated = str(run.updated_at or "")
            prev = seen.get(sid) if isinstance(seen.get(sid), dict) else None
            if prev:
                prev_updated = str(prev.get("job_updated_at") or "")
                # Skip until the run itself advances (new activity / finish).
                if prev_updated and updated and updated <= prev_updated:
                    continue
                if prev.get("last_improved_at") and not updated:
                    continue
                # Legacy cursor (no job_updated_at): terminal sessions stay done.
                if not prev_updated and run.status in TERMINAL_STATUSES and prev.get("last_improved_at"):
                    continue
            claimed.add(sid)
            out.append(
                {
                    "dataset": project_dataset(run.project_id),
                    "session_id": sid,
                    "project_id": run.project_id,
                    "job_updated_at": updated,
                }
            )
            if len(out) >= limit:
                break
        return out
