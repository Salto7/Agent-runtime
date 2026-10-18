"""The periodic half of Dream — curate, maybe synthesize, maybe propose.

Deliberately not a scheduler itself: call ``tick()`` from whatever your host
already uses for periodic work (cron, APScheduler, a queue's periodic actor,
a plain loop with a sleep). Nothing here starts a thread, and nothing here
decides what counts as "not yet curated" — that's the host's cursor to keep,
passed in fresh each tick via ``finished_runs``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from orchestrator.brain import dream
from orchestrator.brain.dream import ChatJSON
from orchestrator.brain.protocols import MemoryStore, ProposalStore, RoleStore


@dataclass
class DreamCycleResult:
    gist_count: int = 0
    synthesis: dict[str, Any] | None = None
    proposals: dict[str, Any] | None = None


def tick(
    *,
    roles: RoleStore,
    memory: MemoryStore,
    proposals: ProposalStore,
    chat_json: ChatJSON,
    finished_runs: Iterable[tuple[str, str, str]] = (),
    min_new_gists: int = dream.SYNTH_MIN_NEW,
) -> DreamCycleResult:
    """One dream cycle.

    ``finished_runs`` is ``(job_id, role_id, transcript)`` for whatever the
    host hasn't curated yet. Synthesis and proposals only run when curation
    actually produced something new, so a quiet period costs nothing beyond
    the (possibly zero) curation calls.
    """
    gist_count = 0
    for job_id, role_id, transcript in finished_runs:
        data = dream.curate_gist(transcript, chat_json=chat_json)
        if data is None:
            continue
        memory.save_lesson(dream.render_gist(job_id, role_id, data))
        gist_count += 1

    synthesis: dict[str, Any] | None = None
    proposal_result: dict[str, Any] | None = None
    if gist_count:
        synthesis = dream.synthesize_playbooks(memory, chat_json=chat_json, min_new=min_new_gists)
        if synthesis.get("ok") and synthesis.get("saved"):
            proposal_result = dream.propose_role_changes(roles, memory, proposals, chat_json=chat_json)

    return DreamCycleResult(gist_count=gist_count, synthesis=synthesis, proposals=proposal_result)
