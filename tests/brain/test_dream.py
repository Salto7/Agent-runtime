from orchestrator.brain import dream
from orchestrator.brain.memory_backend import (
    InMemoryMemoryStore,
    InMemoryProposalStore,
    InMemoryRoleStore,
)
from orchestrator.brain.protocols import ProposalRecord, RoleRecord
from orchestrator.brain.scheduler import tick


def _stub_chat_json(responses: dict[str, dict]):
    """Return canned JSON keyed by a substring of the system prompt."""

    def _chat_json(system: str, human: str) -> dict:
        for key, payload in responses.items():
            if key in system:
                return payload
        raise AssertionError(f"no stub response configured for prompt: {system[:60]!r}")

    return _chat_json


GIST_DATA = {
    "outcome": "success",
    "task_kind": "recon sweep",
    "approach": "Ran a port scan then enumerated services.",
    "worked": ["fast scan completed"],
    "failed": [],
    "plan_advice": ["start with a quick scan before a deep one"],
    "avoid": [],
}


def test_curate_gist_and_render_round_trip():
    chat_json = _stub_chat_json({"curate one agent's work log": GIST_DATA})
    data = dream.curate_gist("some transcript text", chat_json=chat_json)
    assert data == GIST_DATA

    lesson = dream.render_gist("job-abcdef12", "recon", data)
    assert lesson.name == "gist-recon-sweep-job-abcd"
    assert "recon sweep" in lesson.description
    assert "start with a quick scan" in lesson.body


def test_curate_gist_returns_none_on_llm_failure_not_an_exception():
    def _boom(system, human):
        raise RuntimeError("upstream is down")

    assert dream.curate_gist("transcript", chat_json=_boom) is None


def test_synthesize_requires_minimum_gists():
    memory = InMemoryMemoryStore()
    memory.save_lesson(dream.render_gist("job-1", "recon", GIST_DATA))
    result = dream.synthesize_playbooks(memory, chat_json=_stub_chat_json({}), min_new=5)
    assert result == {"ok": False, "reason": "not enough gists yet", "count": 1}


def test_synthesize_saves_playbooks_and_improvements():
    memory = InMemoryMemoryStore()
    for i in range(5):
        memory.save_lesson(dream.render_gist(f"job-{i}", "recon", GIST_DATA))

    synth_payload = {
        "playbooks": [
            {
                "task_kind": "recon sweep",
                "when_to_use": "new target",
                "recommended_plan": ["quick scan", "deep scan"],
                "pitfalls": ["rate limits"],
                "tooling_notes": [],
                "evidence": 5,
            }
        ],
        "platform_improvements": [
            {"issue": "scanner times out often", "seen_in": 3, "suggested_fix": "raise timeout"}
        ],
    }
    chat_json = _stub_chat_json({"consolidate many per-run gists": synth_payload})
    result = dream.synthesize_playbooks(memory, chat_json=chat_json, min_new=5)
    assert result["ok"] is True
    assert set(result["saved"]) == {"playbook-recon-sweep", "platform-improvements"}
    playbooks = [l for l in memory.list_lessons() if l.name.startswith("playbook-")]
    assert len(playbooks) == 1


def test_propose_role_changes_requires_playbooks_first():
    roles = InMemoryRoleStore()
    memory = InMemoryMemoryStore()
    proposals = InMemoryProposalStore()
    result = dream.propose_role_changes(roles, memory, proposals, chat_json=_stub_chat_json({}))
    assert result == {"ok": False, "reason": "no playbooks or platform-improvements yet"}


def test_propose_role_changes_creates_proposal_and_dedupes_repeat_calls():
    roles = InMemoryRoleStore()
    memory = InMemoryMemoryStore()
    memory.save_playbook("playbook-recon-sweep", "recon: new target", "body text")
    proposals = InMemoryProposalStore()

    propose_payload = {
        "proposals": [
            {
                "action": "create",
                "target_id": "",
                "title": "New recon-lead role",
                "rationale": "Playbook recon-sweep has no dedicated role.",
                "role": {
                    "role_id": "recon-lead",
                    "kind": "worker",
                    "aliases": [],
                    "goal": "Own recon sweeps end to end.",
                    "backstory": "You lead recon.",
                },
            }
        ]
    }
    chat_json = _stub_chat_json({"catalog curator": propose_payload})

    first = dream.propose_role_changes(roles, memory, proposals, chat_json=chat_json)
    assert first["ok"] is True
    assert len(first["created"]) == 1

    # A second cycle proposing the same role again must not duplicate it.
    second = dream.propose_role_changes(roles, memory, proposals, chat_json=chat_json)
    assert second["created"] == []
    assert len(proposals.list_proposals(status="pending")) == 1


def test_apply_proposal_approve_creates_role_reject_does_not():
    roles = InMemoryRoleStore()
    proposals = InMemoryProposalStore()
    pending = proposals.create_proposal(
        ProposalRecord(
            id="",
            kind="role",
            action="create",
            target_id="",
            title="t",
            rationale="r",
            payload={"role": {"role_id": "recon-lead", "kind": "worker", "goal": "g", "backstory": "b"}},
        )
    )

    rejected = proposals.create_proposal(
        ProposalRecord(
            id="",
            kind="role",
            action="create",
            target_id="",
            title="t2",
            rationale="r2",
            payload={"role": {"role_id": "never-created", "kind": "worker", "goal": "g"}},
        )
    )
    reject_result = dream.apply_proposal(roles, proposals, rejected.id, decision="reject")
    assert reject_result == {"ok": True, "status": "rejected"}
    assert roles.get_role("never-created") is None

    approve_result = dream.apply_proposal(roles, proposals, pending.id, decision="approve")
    assert approve_result["ok"] is True
    assert roles.get_role("recon-lead") is not None
    assert proposals.get_proposal(pending.id).status == "approved"


def test_apply_proposal_refuses_to_touch_a_protected_role():
    roles = InMemoryRoleStore()
    roles.upsert_role(RoleRecord(role_id="project-manager", protected=True, goal="orig"))
    proposals = InMemoryProposalStore()
    pending = proposals.create_proposal(
        ProposalRecord(
            id="",
            kind="role",
            action="update",
            target_id="project-manager",
            title="t",
            rationale="r",
            payload={"role": {"role_id": "project-manager", "kind": "worker", "goal": "changed"}},
        )
    )
    result = dream.apply_proposal(roles, proposals, pending.id, decision="approve")
    assert result["ok"] is False
    assert roles.get_role("project-manager").goal == "orig"


def test_apply_proposal_rejects_unknown_decision():
    roles = InMemoryRoleStore()
    proposals = InMemoryProposalStore()
    try:
        dream.apply_proposal(roles, proposals, "whatever", decision="maybe")
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_scheduler_tick_runs_full_cycle_end_to_end():
    roles = InMemoryRoleStore()
    memory = InMemoryMemoryStore()
    proposals = InMemoryProposalStore()

    propose_payload = {"proposals": []}
    synth_payload = {
        "playbooks": [
            {
                "task_kind": "recon sweep",
                "when_to_use": "x",
                "recommended_plan": ["a"],
                "pitfalls": [],
                "tooling_notes": [],
                "evidence": 5,
            }
        ],
        "platform_improvements": [],
    }
    chat_json = _stub_chat_json(
        {
            "curate one agent's work log": GIST_DATA,
            "consolidate many per-run gists": synth_payload,
            "catalog curator": propose_payload,
        }
    )
    finished_runs = [(f"job-{i}", "recon", "transcript text") for i in range(5)]

    result = tick(
        roles=roles,
        memory=memory,
        proposals=proposals,
        chat_json=chat_json,
        finished_runs=finished_runs,
        min_new_gists=5,
    )
    assert result.gist_count == 5
    assert result.synthesis["ok"] is True
    assert result.proposals["ok"] is True


def test_scheduler_tick_skips_synthesis_when_nothing_curated():
    result = tick(
        roles=InMemoryRoleStore(),
        memory=InMemoryMemoryStore(),
        proposals=InMemoryProposalStore(),
        chat_json=_stub_chat_json({}),
        finished_runs=(),
    )
    assert result.gist_count == 0
    assert result.synthesis is None
    assert result.proposals is None
