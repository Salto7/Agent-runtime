"""Role registry: a role is an identity a job runs as, plus its skill
bindings. Refreshes itself whenever the role store or the available skill
set changes — call ``get_registry()``/``list_roles()`` whenever you need
current data, no explicit "reload" required.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from orchestrator.brain.memory_backend import InMemoryRoleStore
from orchestrator.brain.protocols import RoleRecord, RoleStore
from orchestrator.utils.service import SharedService


class RoleRegistry(SharedService):
    """Cached view over a ``RoleStore``, invalidated by content or skill changes."""

    def __init__(
        self,
        *,
        store: RoleStore | None = None,
        skill_names: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        self._store = store or InMemoryRoleStore()
        self._skill_names = skill_names
        self._cache: dict[str, RoleRecord] | None = None
        self._stamp: str = ""

    def _stamp_now(self) -> str:
        skills = ",".join(sorted(self._skill_names())) if self._skill_names else ""
        roles = ",".join(
            f"{r.role_id}:{r.updated_at.isoformat()}" for r in self._store.list_roles()
        )
        return f"{skills}|{roles}"

    def get_registry(self) -> dict[str, RoleRecord]:
        stamp = self._stamp_now()
        if self._cache is None or stamp != self._stamp:
            self._cache = {r.role_id: r for r in self._store.list_roles()}
            self._stamp = stamp
        return self._cache

    def get(self, role_id: str) -> RoleRecord | None:
        return self.get_registry().get(role_id)

    def list_roles(self) -> list[RoleRecord]:
        return sorted(self.get_registry().values(), key=lambda r: r.role_id)

    def invalidate(self) -> None:
        self._cache = None
        self._stamp = ""

    def upsert(self, role: RoleRecord) -> RoleRecord:
        saved = self._store.upsert_role(role)
        self.invalidate()
        return saved

    def delete(self, role_id: str) -> None:
        self._store.delete_role(role_id)
        self.invalidate()

    def unbound_skills(self, available_skill_names: Iterable[str]) -> list[str]:
        """Skills no role is bound to yet — what Dream's role proposals act on."""
        bound = {s for r in self.list_roles() for s in r.skills}
        return sorted(set(available_skill_names) - bound)
