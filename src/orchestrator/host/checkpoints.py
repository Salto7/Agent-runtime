"""Job checkpoints — what a resumed agent needs to continue instead of starting cold.

Written when a job finishes, goes idle or is paused. A checkpoint holds the job's
status/result/error and a digest of the project's transcript; ``resume_context_text`` turns the latest one into
the "Resume from checkpoint" block that is prepended to a resumed job's steer text.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from orchestrator.agent.transcript import TranscriptRecord, digest_markdown, read_jsonl
from orchestrator.host.state import JobState

if TYPE_CHECKING:
    from orchestrator.host.control import LocalControlPlane

logger = logging.getLogger(__name__)


def write_checkpoint(plane: "LocalControlPlane", job: JobState, *, reason: str = "job_finished",
                     extra: dict[str, Any] | None = None) -> int:
    """Persist a checkpoint for ``job`` (and mirror it into memory/Cognee as a transcript record)."""
    digest = ""
    try:
        path = plane.workspace(job.project_id) / "transcript.jsonl"
        if path.is_file():
            digest = digest_markdown(read_jsonl(path), limit=40)
    except Exception:  # noqa: BLE001
        digest = ""
    ckpt: dict[str, Any] = {
        "reason": (reason or "job_finished")[:64],
        "job_status": job.status,
        "job_title": job.title,
        "role_ids": list(job.role_ids),
        "error": (job.error or "")[:2000],
        "result_preview": (job.result or "")[:2000],
        "parent_id": job.parent_id,
        "objective_seq": job.objective_seq,
        "transcript_digest": digest[:16000],
        **(extra or {}),
    }
    seq = plane.store.add_checkpoint(job.id, ckpt)
    try:
        from orchestrator.crew.memory import MemoryService

        detail = digest or ckpt["result_preview"]
        MemoryService.remember_transcript(
            type("J", (), {"project_id": job.project_id, "id": job.id, "objective_id": str(job.objective_seq or "")})(),
            TranscriptRecord(
                category="checkpoint",
                summary="\n".join(b for b in (f"checkpoint seq={seq} reason={ckpt['reason']}",
                                              f"job={job.title} status={job.status}", detail[:2000]) if b),
                event=ckpt["reason"], role_id=(job.role_ids or [""])[0], job_id=job.id,
                project_id=job.project_id,
            ),
        )
    except Exception:  # noqa: BLE001
        logger.debug("checkpoint memory write failed for %s", job.id, exc_info=True)
    return seq


def resume_context_text(plane: "LocalControlPlane", job_id: str) -> str:
    """Continuation block from the latest checkpoint ('' when the job never checkpointed)."""
    ckpt = plane.store.latest_checkpoint(job_id)
    if not ckpt:
        return ""
    lines = [
        "## Resume from checkpoint",
        f"checkpoint_seq={ckpt.get('seq')} reason={ckpt.get('reason')}",
        f"prior_status={ckpt.get('job_status')}",
    ]
    if ckpt.get("error"):
        lines.append(f"prior_error={ckpt['error']}")
    if ckpt.get("result_preview"):
        lines.append(f"prior_result={ckpt['result_preview']}")
    if ckpt.get("transcript_digest"):
        lines += ["", "### Transcript digest", str(ckpt["transcript_digest"])[:4000]]
    lines.append("Continue from your last work; do not repeat completed work.")
    return "\n".join(lines)
