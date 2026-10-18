from orchestrator.brain.memory_backend import InMemoryRoleStore
from orchestrator.brain.protocols import RoleRecord
from orchestrator.brain.roles import RoleRegistry


def test_registry_reflects_store_without_explicit_reload():
    store = InMemoryRoleStore()
    reg = RoleRegistry(store=store)
    assert reg.list_roles() == []

    store.upsert_role(RoleRecord(role_id="recon", goal="find things"))
    # No reg.invalidate() call — get_registry() must notice the store changed.
    assert [r.role_id for r in reg.list_roles()] == ["recon"]


def test_registry_cache_is_reused_when_nothing_changed():
    store = InMemoryRoleStore()
    store.upsert_role(RoleRecord(role_id="recon", goal="find things"))
    reg = RoleRegistry(store=store)
    first = reg.get_registry()
    second = reg.get_registry()
    assert first is second  # same dict object: no rebuild happened


def test_registry_refreshes_when_skill_set_changes():
    store = InMemoryRoleStore()
    store.upsert_role(RoleRecord(role_id="recon", goal="find things", skills=("nmap",)))
    current_skills = {"nmap"}
    reg = RoleRegistry(store=store, skill_names=lambda: current_skills)
    first = reg.get_registry()

    current_skills.add("httpx")  # a skill got added — "periodic update of roles"
    second = reg.get_registry()
    assert first is not second  # stamp changed, registry rebuilt
    assert second["recon"].role_id == "recon"


def test_unbound_skills_finds_skills_no_role_claims():
    store = InMemoryRoleStore()
    store.upsert_role(RoleRecord(role_id="recon", skills=("nmap",)))
    reg = RoleRegistry(store=store)
    assert reg.unbound_skills(["nmap", "httpx", "ffuf"]) == ["ffuf", "httpx"]


def test_upsert_invalidates_cache_immediately():
    reg = RoleRegistry()
    reg.get_registry()  # populate cache
    reg.upsert(RoleRecord(role_id="new-role", goal="g"))
    assert "new-role" in reg.get_registry()
