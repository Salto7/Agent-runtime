"""Platform catalog: the only application access to catalog tables.

Roles, skills, MCP server specs, file bodies, and role mappings live in one
SQLite file (``<platform_workspace_dir>/catalog.sqlite3``, see
``orchestrator.catalog.store``). Callers use this module. They do not query
those tables directly.

``skills/`` is a one-time bootstrap when the skill table is empty. Pack type
(peon vs claude) is inferred per directory. After that the database is the
registry.

A host with its own storage subclasses :class:`PlatformCatalog` and installs it
with :meth:`PlatformCatalog.set_shared`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.catalog.store import IntegrityError, atomic, configure_store, latest_stamp
from orchestrator.config import get_config

logger = logging.getLogger(__name__)

OWNER_SKILL = "skill"
OWNER_MCP = "mcp"
_AFFINITY_THRESHOLD = 0.5
_SKILLS_FINGERPRINT = ""  # last on-disk skill state reconciled by this process
_revision: float = 0.0


@dataclass(frozen=True)
class Binding:
    role_id: str
    owner_kind: str
    owner_id: str
    score: float = 1.0
    lead: bool = False


@dataclass(frozen=True)
class SkillRecord:
    skill_id: str
    provider: str
    description: str
    body: str
    manifest: dict[str, Any]
    setup: dict[str, Any] | None
    files: dict[str, str] = field(default_factory=dict)
    bindings: tuple[Binding, ...] = ()


@dataclass(frozen=True)
class McpRecord:
    mcp_id: str
    description: str
    transport: str
    command: str
    args: tuple[str, ...]
    cwd: str
    install: dict[str, Any] | None
    env_schema: tuple[tuple[str, bool], ...]
    enabled: bool
    files: dict[str, str] = field(default_factory=dict)
    bindings: tuple[Binding, ...] = ()
    reference_hosts: tuple[str, ...] = ()
    # Full per-field metadata (type/description/default/choices) behind
    # env_schema's plain (name, required) pairs — see orchestrator/mcp/parse.py.
    env_schema_full: tuple[dict[str, Any], ...] = ()


def _bump() -> float:
    global _revision
    _revision = max(_revision + 0.001, time.time())
    return _revision


def _invalidate() -> None:
    _bump()
    try:
        from orchestrator.crew.roles.registry import RoleRegistry
        from orchestrator.skills.registry import SkillRegistry

        SkillRegistry.shared().invalidate()
        RoleRegistry.shared().invalidate()
    except Exception:
        logger.warning("catalog cache invalidate skipped", exc_info=True)


def _hash_payload(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _safe_rel(rel: str) -> str:
    rel_s = str(rel or "").replace("\\", "/").lstrip("/")
    if not rel_s or rel_s.startswith("/") or ".." in rel_s.split("/"):
        raise ValueError(f"unsafe path {rel!r}")
    return rel_s


class PlatformCatalog:
    """CRUD for the platform skill, role, and MCP catalogs."""

    _shared: "PlatformCatalog | None" = None

    @classmethod
    def shared(cls) -> "PlatformCatalog":
        if cls._shared is None:
            cls._shared = cls()
            cls._shared.open()
        return cls._shared

    @classmethod
    def set_shared(cls, catalog: "PlatformCatalog | None") -> None:
        """Install a host-provided catalog (or ``None`` to rebuild the default)."""
        PlatformCatalog._shared = catalog

    def open(self) -> None:
        """Point the default SQLite store at the configured platform workspace."""
        configure_store(Path(get_config().platform_workspace_dir) / "catalog.sqlite3")

    def revision(self) -> float:
        """Stamp registries use to drop a stale in-process cache."""
        from orchestrator.catalog.models import (
            CatalogBinding,
            CatalogMcp,
            CatalogSkill,
            EngagementRole,
        )

        try:
            stamps = [float(latest_stamp(m)) for m in (EngagementRole, CatalogSkill, CatalogMcp, CatalogBinding)]
        except Exception:
            stamps = []
        return max([_revision, *stamps], default=_revision)

    # --- roles -------------------------------------------------------------

    def ensure_seeded(self) -> None:
        """Insert default roles and, once, bootstrap skills/MCPs from disk."""
        self._seed_roles()
        self._import_skill_trees()
        self._import_mcp_packs()

    def _seed_roles(self) -> int:
        from orchestrator.crew.roles.catalog import DEFAULT_ROLES

        from orchestrator.catalog.models import EngagementRole

        try:
            if EngagementRole.objects.exists():
                return 0
        except Exception as exc:
            logger.debug("role table unavailable: %s", exc)
            return 0
        created = 0
        try:
            with atomic():
                for role in DEFAULT_ROLES:
                    _, was =                     EngagementRole.objects.get_or_create(
                        role_id=role.id,
                        defaults={
                            "kind": role.kind,
                            "aliases": list(role.aliases),
                            "protected": bool(role.protected),
                            "goal": role.goal,
                            "backstory": role.backstory,
                        },
                    )
                    if was:
                        created += 1
        except IntegrityError:
            return 0
        if created:
            _invalidate()
        return created

    def list_roles(self):
        from orchestrator.crew.roles.catalog import DEFAULT_ROLES, RoleDef

        from orchestrator.catalog.models import EngagementRole

        try:
            self._seed_roles()
            rows = EngagementRole.objects.all().order_by("role_id")
            return tuple(
                RoleDef(
                    id=str(row.role_id),
                    kind=str(row.kind),
                    aliases=tuple(
                        str(a) for a in (row.aliases or []) if str(a).strip()
                    ),
                    protected=bool(row.protected),
                    goal=str(row.goal or ""),
                    backstory=str(row.backstory or ""),
                )
                for row in rows
            )
        except Exception as exc:
            logger.debug("list_roles failed: %s", exc)
            return DEFAULT_ROLES

    def get_role(self, role_id: str):
        from orchestrator.crew.roles.catalog import normalize_role_id

        rid = normalize_role_id(role_id)
        if not rid:
            return None
        for row in self.list_roles():
            if row.id == rid or rid in row.aliases:
                return row
        return None

    def upsert_role(
        self,
        *,
        role_id: str,
        kind: str,
        aliases: list[str] | tuple[str, ...] | str = (),
        protected: bool | None = None,
        previous_id: str = "",
        goal: str | None = None,
        backstory: str | None = None,
    ):
        from orchestrator.crew.roles.catalog import (
            BOOKEND_KINDS,
            normalize_role_id,
            role_def_from_mapping,
        )

        from orchestrator.catalog.models import EngagementRole

        defn = role_def_from_mapping(
            {
                "id": role_id,
                "kind": kind,
                "aliases": aliases,
                "protected": bool(protected) if protected is not None else False,
            }
        )
        previous = normalize_role_id(previous_id) if previous_id else ""
        if previous == defn.id:
            previous = ""
        if previous:
            old = EngagementRole.objects.filter(role_id=previous).first()
            if old is None:
                raise ValueError(f"unknown role {previous!r}")
            if bool(old.protected) or old.kind in BOOKEND_KINDS:
                raise ValueError(f"role {previous!r} is protected and cannot be renamed")
            if EngagementRole.objects.filter(role_id=defn.id).exists():
                raise ValueError(f"role {defn.id!r} already exists")
        existing = EngagementRole.objects.filter(role_id=defn.id).first()
        if existing is not None:
            current_protected = bool(existing.protected) or existing.kind in BOOKEND_KINDS
            if current_protected and defn.kind != existing.kind:
                raise ValueError(
                    f"role {defn.id!r} is protected; kind cannot change from "
                    f"{existing.kind!r}"
                )
            keep_protected = bool(existing.protected)
            final_kind = existing.kind if current_protected else defn.kind
        else:
            keep_protected = defn.protected
            final_kind = defn.kind

        if protected is not None and not (existing and keep_protected and existing.kind in BOOKEND_KINDS):
            keep_protected = bool(protected)
        if final_kind in BOOKEND_KINDS:
            keep_protected = True
            clash = (
                EngagementRole.objects.filter(kind=final_kind)
                .exclude(role_id=defn.id)
                .first()
            )
            if clash is not None:
                raise ValueError(
                    f"kind {final_kind!r} already used by role {clash.role_id!r}"
                )

        self._seed_roles()
        for other in EngagementRole.objects.all().iterator():
            if other.role_id in {defn.id, previous}:
                continue
            other_aliases = tuple(
                str(a).strip().lower() for a in (other.aliases or []) if str(a).strip()
            )
            conflict = {other.role_id, *other_aliases} & {defn.id, *defn.aliases}
            if conflict:
                raise ValueError(
                    f"alias/id conflict with role {other.role_id!r}: {sorted(conflict)}"
                )

        with atomic():
            from orchestrator.crew.roles.catalog import seed_role_text

            source = existing
            if source is None and previous:
                source = EngagementRole.objects.filter(role_id=previous).first()
            seeded_goal, seeded_story = seed_role_text(defn.id, final_kind)
            if goal is None:
                goal_text = (source.goal if source is not None else "") or seeded_goal
            else:
                goal_text = str(goal).strip() or seeded_goal
            if backstory is None:
                story_text = (
                    source.backstory if source is not None else ""
                ) or seeded_story
            else:
                story_text = str(backstory).strip() or seeded_story
            EngagementRole.objects.update_or_create(
                role_id=defn.id,
                defaults={
                    "kind": final_kind,
                    "aliases": list(defn.aliases),
                    "protected": keep_protected,
                    "goal": goal_text,
                    "backstory": story_text,
                },
            )
            if previous:
                from orchestrator.catalog.models import CatalogBinding, RoleSkillMemory

                CatalogBinding.objects.filter(role_id=previous).update(role_id=defn.id)
                memory = RoleSkillMemory.objects.filter(role_id=previous).first()
                if memory is not None:
                    RoleSkillMemory.objects.update_or_create(
                        role_id=defn.id,
                        defaults={
                            "skills": memory.skills,
                            "counts": memory.counts,
                            "last_source": memory.last_source,
                            "last_project_id": memory.last_project_id,
                        },
                    )
                    memory.delete()
                EngagementRole.objects.filter(role_id=previous).delete()
        _invalidate()
        return self.get_role(defn.id)

    def delete_role(self, role_id: str) -> None:
        from orchestrator.crew.roles.catalog import normalize_role_id

        from orchestrator.catalog.models import CatalogBinding, EngagementRole

        rid = normalize_role_id(role_id)
        if not rid:
            raise ValueError("invalid role id")
        row = EngagementRole.objects.filter(role_id=rid).first()
        if row is None:
            raise KeyError(f"unknown role {rid!r}")
        current = self.get_role(rid)
        if current is not None and current.is_protected:
            raise ValueError(f"role {rid!r} is protected and cannot be deleted")
        with atomic():
            CatalogBinding.objects.filter(role_id=rid).delete()
            row.delete()
        _invalidate()

    # --- bindings ----------------------------------------------------------

    def list_bindings(
        self,
        *,
        role_id: str = "",
        owner_kind: str = "",
        owner_id: str = "",
    ) -> tuple[Binding, ...]:
        from orchestrator.catalog.models import CatalogBinding

        qs = CatalogBinding.objects.all()
        if role_id:
            qs = qs.filter(role_id=role_id.strip().lower())
        if owner_kind:
            qs = qs.filter(owner_kind=owner_kind.strip().lower())
        if owner_id:
            qs = qs.filter(owner_id=owner_id.strip())
        try:
            rows = qs.order_by("owner_kind", "owner_id", "role_id")
            return tuple(
                Binding(
                    role_id=str(row.role_id),
                    owner_kind=str(row.owner_kind),
                    owner_id=str(row.owner_id),
                    score=float(row.score),
                    lead=bool(row.lead),
                )
                for row in rows
            )
        except Exception as exc:
            logger.debug("list_bindings failed: %s", exc)
            return ()

    def replace_bindings(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        bindings: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    ) -> tuple[Binding, ...]:
        """Replace every role mapping for one skill or MCP server."""
        from orchestrator.catalog.models import CatalogBinding

        kind = str(owner_kind or "").strip().lower()
        oid = str(owner_id or "").strip()
        if kind not in {OWNER_SKILL, OWNER_MCP}:
            raise ValueError("owner_kind must be skill or mcp")
        if not oid:
            raise ValueError("owner id is required")
        cleaned: list[Binding] = []
        seen: set[str] = set()
        for raw in bindings or ():
            if not isinstance(raw, dict):
                raise ValueError("binding must be an object")
            from orchestrator.crew.roles.catalog import normalize_role_id

            rid = normalize_role_id(raw.get("role_id"))
            if not rid:
                raise ValueError("binding role_id is invalid")
            if rid in seen:
                raise ValueError(f"duplicate binding for role {rid!r}")
            seen.add(rid)
            if self.get_role(rid) is None:
                raise ValueError(f"unknown role {rid!r}")
            score = float(raw.get("score", 1.0))
            if score < 0 or score > 1:
                raise ValueError("score must be between 0 and 1")
            cleaned.append(
                Binding(rid, kind, oid, score, bool(raw.get("lead")))
            )
        leads = [b for b in cleaned if b.lead]
        if len(leads) > 1:
            raise ValueError("at most one binding may be lead")
        with atomic():
            CatalogBinding.objects.filter(owner_kind=kind, owner_id=oid).delete()
            CatalogBinding.objects.bulk_create(
                [
                    CatalogBinding(
                        role_id=b.role_id,
                        owner_kind=kind,
                        owner_id=oid,
                        score=b.score,
                        lead=b.lead,
                    )
                    for b in cleaned
                ]
            )
        _invalidate()
        return tuple(cleaned)

    def _apply_owner_bindings(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        bindings: list[dict[str, Any]] | None,
        rename_from: str = "",
        infer_id: str = "",
        infer_description: str = "",
        infer_body: str = "",
        infer_tags: tuple[str, ...] = (),
    ) -> None:
        """Write explicit bindings, else rename carry, else auto-infer roles.

        ``bindings is None`` → auto-infer (install / omit).
        Non-empty list → replace with those rows.
        ``bindings == []`` → clear when mappings already exist; otherwise auto-infer
        (catalog UI always posts an empty list for new items).
        """
        if bindings:
            self.replace_bindings(
                owner_kind=owner_kind, owner_id=owner_id, bindings=bindings
            )
            return
        if bindings is not None:
            existing = self.list_bindings(owner_kind=owner_kind, owner_id=owner_id)
            if existing:
                self.replace_bindings(
                    owner_kind=owner_kind, owner_id=owner_id, bindings=[]
                )
                return
        if rename_from and rename_from != owner_id:
            carried = self.list_bindings(owner_kind=owner_kind, owner_id=rename_from)
            if carried:
                self.replace_bindings(
                    owner_kind=owner_kind,
                    owner_id=owner_id,
                    bindings=[
                        {
                            "role_id": b.role_id,
                            "score": b.score,
                            "lead": b.lead,
                        }
                        for b in carried
                    ],
                )
                return
        try:
            from orchestrator.skills.affinity import infer_catalog_bindings

            inferred = infer_catalog_bindings(
                owner_id=infer_id or owner_id,
                owner_kind=owner_kind,
                description=infer_description,
                body=infer_body,
                tags=infer_tags,
            )
        except Exception as exc:
            logger.warning(
                "auto-bind failed for %s %s: %s", owner_kind, owner_id, exc
            )
            inferred = []
        if inferred:
            try:
                self.replace_bindings(
                    owner_kind=owner_kind, owner_id=owner_id, bindings=inferred
                )
                return
            except ValueError as exc:
                logger.warning(
                    "auto-bind rejected for %s %s: %s", owner_kind, owner_id, exc
                )
        _invalidate()

    # --- skills ------------------------------------------------------------

    def list_skills(self) -> tuple[SkillRecord, ...]:
        from orchestrator.catalog.models import CatalogSkill

        try:
            self._import_skill_trees()
            rows = CatalogSkill.objects.all().order_by("skill_id")
        except Exception as exc:
            logger.debug("list_skills failed: %s", exc)
            return ()
        bindings = self.list_bindings(owner_kind=OWNER_SKILL)
        by_owner: dict[str, list[Binding]] = {}
        for binding in bindings:
            by_owner.setdefault(binding.owner_id, []).append(binding)
        return tuple(
            SkillRecord(
                skill_id=str(row.skill_id),
                provider=str(row.provider or "peon"),
                description=str(row.description or ""),
                body=str(row.body or ""),
                manifest=dict(row.manifest or {}),
                setup=dict(row.setup) if isinstance(row.setup, dict) else None,
                files={},
                bindings=tuple(by_owner.get(str(row.skill_id), ())),
            )
            for row in rows
        )

    def get_skill(self, skill_id: str) -> SkillRecord | None:
        from orchestrator.catalog.models import CatalogFile, CatalogSkill

        sid = str(skill_id or "").strip()
        if not sid:
            return None
        try:
            row = CatalogSkill.objects.filter(skill_id=sid).first()
        except Exception as exc:
            logger.debug("get_skill failed: %s", exc)
            return None
        if row is None and "/" not in sid:
            row = CatalogSkill.objects.filter(skill_id=sid).first()
        if row is None:
            return None
        files = {
            str(item.path): str(item.body or "")
            for item in CatalogFile.objects.filter(
                owner_kind=OWNER_SKILL, owner_id=row.skill_id
            )
        }
        bindings = self.list_bindings(owner_kind=OWNER_SKILL, owner_id=row.skill_id)
        return SkillRecord(
            skill_id=str(row.skill_id),
            provider=str(row.provider or "peon"),
            description=str(row.description or ""),
            body=str(row.body or ""),
            manifest=dict(row.manifest or {}),
            setup=dict(row.setup) if isinstance(row.setup, dict) else None,
            files=files,
            bindings=bindings,
        )

    def upsert_skill(
        self,
        *,
        skill_md: str,
        files: dict[str, str] | None = None,
        skill_id: str = "",
        provider: str = "peon",
        bindings: list[dict[str, Any]] | None = None,
        keep_bindings: bool = False,
        from_disk: bool = False,
        only_if_pristine: bool = False,
    ) -> SkillRecord:
        """Create/update one catalog skill.

        ``from_disk`` stamps the row with the digest it was imported at
        (``manifest["_imported_hash"]``). A row without that stamp, or whose
        content has moved on from it, was authored/edited in the catalog.
        ``only_if_pristine`` makes a disk refresh leave such rows alone.
        """
        from orchestrator.skills.manifest import ParseProfile, SkillParseError, parse_skill_manifest
        from orchestrator.skills.providers.base import PEON_PROVIDER, SkillId

        from orchestrator.catalog.models import CatalogFile, CatalogSkill

        md = str(skill_md or "").strip()
        if not md:
            raise ValueError("skill_md is required")
        file_map: dict[str, str] = {}
        for key, value in (files or {}).items():
            rel = _safe_rel(str(key))
            if rel == "SKILL.md" or str(value).startswith("<<binary omitted:"):
                continue
            file_map[rel] = str(value)
        hinted = str(skill_id or "").strip()
        parsed_id = SkillId.parse(hinted) if hinted else None
        # Catalog rows have no directory. Validate against the manifest name
        # so create and rename are not rejected as a folder mismatch.
        named = re.search(r"(?m)^name:\s*([A-Za-z0-9][A-Za-z0-9-]*)\s*$", md)
        directory = (
            named.group(1).lower()
            if named
            else (parsed_id.name if parsed_id is not None else (hinted or "skill"))
        )
        profile = (
            ParseProfile.CLAUDE_CODE
            if (parsed_id is not None and parsed_id.provider != PEON_PROVIDER)
            or provider != PEON_PROVIDER
            else ParseProfile.PEON
        )
        try:
            manifest = parse_skill_manifest(
                Path(directory) / "SKILL.md",
                profile=profile,
                text=md if md.endswith("\n") else md + "\n",
            )
        except SkillParseError as exc:
            raise ValueError(exc.message) from exc
        public = manifest.command_name
        prov = PEON_PROVIDER if profile == ParseProfile.PEON else "claude"
        if prov != PEON_PROVIDER:
            public = f"{prov}/{manifest.command_name}"
        rename_from = ""
        if hinted and SkillId.parse(hinted) is not None:
            want = SkillId.parse(hinted)
            assert want is not None
            if want.name != manifest.command_name:
                if CatalogSkill.objects.filter(skill_id=public).exists():
                    raise ValueError(f"skill {public!r} already exists")
                rename_from = want.public
            else:
                public = want.public
                prov = want.provider
        payload = _manifest_payload(manifest)
        payload["_source"] = md if md.endswith("\n") else md + "\n"
        setup = _setup_from_files(file_map)
        post = _setup_post(manifest.metadata)
        if setup is not None and post:
            setup = {**setup, "post": post}
        digest = _hash_payload({"md": md, "files": file_map, "provider": prov})
        current = CatalogSkill.objects.filter(skill_id=public).first()
        existed = current is not None
        if only_if_pristine and current is not None:
            if current.content_hash == digest:
                if from_disk and not (current.manifest or {}).get("_imported_hash"):
                    # Adopt: identical to disk, so it is a pristine import.
                    current.manifest = {
                        **(current.manifest or {}),
                        "_imported_hash": digest,
                    }
                    current.save(update_fields=["manifest"])
                return self.get_skill(public)  # already current
            stamp = (current.manifest or {}).get("_imported_hash")
            if not stamp or stamp != current.content_hash:
                logger.warning(
                    "skill %s differs from disk but was edited in the catalog; "
                    "not overwriting (use 'sync from disk' to force)",
                    public,
                )
                return self.get_skill(public)
        if from_disk:
            payload["_imported_hash"] = digest
        with atomic():
            CatalogSkill.objects.update_or_create(
                skill_id=public,
                defaults={
                    "provider": prov,
                    "description": manifest.description,
                    "body": manifest.body,
                    "manifest": payload,
                    "setup": setup,
                    "content_hash": digest,
                },
            )
            CatalogFile.objects.filter(
                owner_kind=OWNER_SKILL, owner_id=public
            ).delete()
            CatalogFile.objects.bulk_create(
                [
                    CatalogFile(
                        owner_kind=OWNER_SKILL,
                        owner_id=public,
                        path=rel,
                        body=text,
                    )
                    for rel, text in sorted(file_map.items())
                ]
            )
        if keep_bindings and existed and not rename_from:
            _invalidate()
        else:
            self._apply_owner_bindings(
                owner_kind=OWNER_SKILL,
                owner_id=public,
                bindings=bindings,
                rename_from=rename_from if rename_from != public else "",
                infer_id=public,
                infer_description=manifest.description,
                infer_body=manifest.body,
                infer_tags=tuple(manifest.tags or ()),
            )
        if rename_from and rename_from != public:
            self.delete_skill(rename_from)
        rec = self.get_skill(public)
        if rec is None:
            raise RuntimeError(f"skill {public!r} missing after upsert")
        return rec

    def delete_skill(self, skill_id: str) -> None:
        from orchestrator.catalog.models import CatalogBinding, CatalogFile, CatalogSkill

        sid = str(skill_id or "").strip()
        row = CatalogSkill.objects.filter(skill_id=sid).first()
        if row is None:
            raise KeyError(f"unknown skill {sid!r}")
        with atomic():
            CatalogFile.objects.filter(owner_kind=OWNER_SKILL, owner_id=sid).delete()
            CatalogBinding.objects.filter(owner_kind=OWNER_SKILL, owner_id=sid).delete()
            row.delete()
        _invalidate()

    # --- mcp ---------------------------------------------------------------

    def list_mcps(self) -> tuple[McpRecord, ...]:
        from orchestrator.catalog.models import CatalogMcp

        try:
            rows = list(CatalogMcp.objects.all().order_by("mcp_id"))
        except Exception as exc:
            logger.debug("list_mcps failed: %s", exc)
            return ()
        bindings = self.list_bindings(owner_kind=OWNER_MCP)
        by_owner: dict[str, list[Binding]] = {}
        for binding in bindings:
            by_owner.setdefault(binding.owner_id, []).append(binding)
        return tuple(_mcp_from_row(row, tuple(by_owner.get(row.mcp_id, ())), {}) for row in rows)

    def get_mcp(self, mcp_id: str) -> McpRecord | None:
        from orchestrator.catalog.models import CatalogFile, CatalogMcp

        mid = str(mcp_id or "").strip().lower()
        if not mid:
            return None
        try:
            row = CatalogMcp.objects.filter(mcp_id=mid).first()
        except Exception as exc:
            logger.debug("get_mcp failed: %s", exc)
            return None
        if row is None:
            return None
        files = {
            str(item.path): str(item.body or "")
            for item in CatalogFile.objects.filter(owner_kind=OWNER_MCP, owner_id=mid)
        }
        bindings = self.list_bindings(owner_kind=OWNER_MCP, owner_id=mid)
        return _mcp_from_row(row, bindings, files)

    def upsert_mcp(
        self,
        document: dict[str, Any] | str,
        *,
        files: dict[str, str] | None = None,
        bindings: list[dict[str, Any]] | None = None,
        enabled: bool | None = None,
        rename_from: str = "",
        keep_bindings: bool = False,
    ) -> McpRecord:
        from orchestrator.mcp.parse import parse_mcp_document

        from orchestrator.catalog.models import CatalogFile, CatalogMcp

        specs = parse_mcp_document(document)
        if len(specs) != 1:
            raise ValueError("upsert_mcp accepts exactly one server")
        spec = specs[0]
        previous = str(rename_from or "").strip().lower()
        if previous in {"", "__new__", spec.mcp_id}:
            previous = ""
        if previous:
            if not CatalogMcp.objects.filter(mcp_id=previous).exists():
                raise ValueError(f"unknown mcp server {previous!r}")
            if CatalogMcp.objects.filter(mcp_id=spec.mcp_id).exists():
                raise ValueError(f"mcp server {spec.mcp_id!r} already exists")
        existing = CatalogMcp.objects.filter(mcp_id=spec.mcp_id).first()
        existed = existing is not None
        keep_enabled = bool(existing.enabled) if existing is not None else True
        if enabled is not None:
            keep_enabled = bool(enabled)
        # None = leave files; {} = clear; dict = replace.
        replace_files = files is not None
        if replace_files:
            file_map = {_safe_rel(k): str(v) for k, v in files.items()}
        else:
            file_map = {
                str(item.path): str(item.body or "")
                for item in CatalogFile.objects.filter(
                    owner_kind=OWNER_MCP, owner_id=spec.mcp_id
                )
            }
        digest = _hash_payload(
            {
                "command": spec.command,
                "args": list(spec.args),
                "cwd": spec.cwd,
                "install": spec.install,
                "env_schema": spec.env_schema_full,
                "reference_hosts": list(spec.reference_hosts),
                "files": file_map,
            }
        )
        schema = list(spec.env_schema_full)
        with atomic():
            CatalogMcp.objects.update_or_create(
                mcp_id=spec.mcp_id,
                defaults={
                    "description": spec.description,
                    "transport": spec.transport,
                    "command": spec.command,
                    "args": list(spec.args),
                    "cwd": spec.cwd,
                    "install": spec.install,
                    "env_schema": schema,
                    "reference_hosts": list(spec.reference_hosts),
                    "enabled": keep_enabled,
                    "content_hash": digest,
                },
            )
            if replace_files:
                CatalogFile.objects.filter(
                    owner_kind=OWNER_MCP, owner_id=spec.mcp_id
                ).delete()
                if file_map:
                    CatalogFile.objects.bulk_create(
                        [
                            CatalogFile(
                                owner_kind=OWNER_MCP,
                                owner_id=spec.mcp_id,
                                path=rel,
                                body=text,
                            )
                            for rel, text in sorted(file_map.items())
                        ]
                    )
        if keep_bindings and existed and not previous:
            _invalidate()
        else:
            self._apply_owner_bindings(
                owner_kind=OWNER_MCP,
                owner_id=spec.mcp_id,
                bindings=bindings,
                rename_from=previous,
                infer_id=spec.mcp_id,
                infer_description=spec.description,
                infer_body=" ".join(
                    (
                        spec.command,
                        " ".join(spec.args),
                        spec.cwd,
                        " ".join(name for name, _req in spec.env_schema),
                    )
                ),
                infer_tags=(),
            )
        if previous:
            self.delete_mcp(previous)
        rec = self.get_mcp(spec.mcp_id)
        if rec is None:
            raise RuntimeError(f"mcp {spec.mcp_id!r} missing after upsert")
        return rec

    def save_mcp_document(
        self,
        document: dict[str, Any] | str,
        *,
        files: dict[str, str] | None = None,
        bindings: list[dict[str, Any]] | None = None,
        enabled: bool | None = None,
        rename_from: str = "",
    ) -> tuple[McpRecord, ...]:
        """Create or replace one server, or update every entry in an mcpServers map."""
        from orchestrator.mcp.parse import parse_mcp_document

        specs = parse_mcp_document(document)
        if len(specs) == 1:
            return (
                self.upsert_mcp(
                    document,
                    files=files,
                    bindings=bindings,
                    enabled=enabled,
                    rename_from=rename_from,
                ),
            )
        if files or bindings or rename_from:
            raise ValueError(
                "an mcpServers map updates each named server; "
                "save files, bindings, and renames on one server"
            )
        saved: list[McpRecord] = []
        for spec in specs:
            body: dict[str, Any] = {
                "name": spec.mcp_id,
                "description": spec.description,
                "transport": spec.transport,
                "command": spec.command,
                "args": list(spec.args),
                "cwd": spec.cwd,
                "env_schema": list(spec.env_schema_full),
            }
            if spec.install:
                body["install"] = spec.install
            saved.append(self.upsert_mcp(body, enabled=enabled))
        return tuple(saved)

    def delete_mcp(self, mcp_id: str) -> None:
        from orchestrator.catalog.models import CatalogBinding, CatalogFile, CatalogMcp

        mid = str(mcp_id or "").strip().lower()
        row = CatalogMcp.objects.filter(mcp_id=mid).first()
        if row is None:
            raise KeyError(f"unknown mcp server {mid!r}")
        with atomic():
            CatalogFile.objects.filter(owner_kind=OWNER_MCP, owner_id=mid).delete()
            CatalogBinding.objects.filter(owner_kind=OWNER_MCP, owner_id=mid).delete()
            row.delete()
        _invalidate()

    # --- bootstrap / disk sync ---------------------------------------------

    def _skill_disk_roots(self) -> list[tuple[Path, str]]:
        cfg = get_config()
        roots: list[tuple[Path, str]] = []
        skills = Path(cfg.skills_dir)
        imported = Path(cfg.skills_imported_dir or cfg.skills_dir)
        if skills.is_dir():
            roots.append((skills, "infer"))
        if imported.is_dir() and imported.resolve() != skills.resolve():
            roots.append((imported, "claude"))
        for extra in cfg.skills_extra_dirs or ():
            path = Path(extra)
            if path.is_dir():
                roots.append((path, "claude"))
        return roots

    def _import_skill_trees(self) -> int:
        from orchestrator.catalog.models import CatalogSkill

        try:
            if CatalogSkill.objects.exists():
                return 0
        except Exception as exc:
            logger.debug("skill table unavailable: %s", exc)
            return 0
        self._seed_roles()
        count = 0
        for root, mode in self._skill_disk_roots():
            count += len(
                self._import_skill_root(root, mode, keep_bindings=False)
            )
        if count:
            logger.info("bootstrapped %s skills into the platform catalog", count)
        return count

    def sync_skills_from_disk(self) -> dict[str, list[str]]:
        """Re-read ``skills/`` into the catalog. Preserves existing role bindings."""
        from orchestrator.catalog.models import CatalogSkill

        self._seed_roles()
        before = {
            str(row.skill_id): str(row.content_hash or "")
            for row in CatalogSkill.objects.all()
        }
        seen: set[str] = set()
        for root, mode in self._skill_disk_roots():
            for skill_id in self._import_skill_root(
                root, mode, keep_bindings=True
            ):
                seen.add(skill_id)
        after = {
            str(row.skill_id): str(row.content_hash or "")
            for row in CatalogSkill.objects.filter(skill_id__in=seen)
        }
        added = sorted(sid for sid in seen if sid not in before)
        modified = sorted(
            sid
            for sid in seen
            if sid in before and before[sid] != after.get(sid, "")
        )
        # Disk is additive: catalog-only skills (UI-authored) are not removed.
        return {"added": added, "removed": [], "modified": modified}

    def _skills_disk_fingerprint(self) -> str:
        """Cheap change detector for the on-disk skill trees (path/mtime/size)."""
        h = hashlib.sha256()
        for root, mode in self._skill_disk_roots():
            h.update(f"{root}:{mode}".encode())
            for path in sorted(root.rglob("*")):
                try:
                    if path.is_file() and not any(
                        part.startswith(".") or part == "__pycache__"
                        for part in path.relative_to(root).parts
                    ):
                        st = path.stat()
                        h.update(
                            f"{path.relative_to(root).as_posix()}:{st.st_mtime_ns}:{st.st_size}".encode()
                        )
                except OSError:
                    continue
        return h.hexdigest()

    def refresh_stale_skills(self, *, force: bool = False) -> dict[str, list[str]]:
        """Bring catalog skills up to date with ``skills/`` before work runs.

        Only rows that are pristine imports are refreshed; skills edited in the
        catalog (UI) or authored there are never overwritten. A fingerprint of
        the disk trees makes the common no-change case a cheap stat walk.
        """
        global _SKILLS_FINGERPRINT
        try:
            fingerprint = self._skills_disk_fingerprint()
        except Exception as exc:  # noqa: BLE001
            logger.debug("skill fingerprint failed: %s", exc)
            return {"added": [], "removed": [], "modified": []}
        if not force and fingerprint == _SKILLS_FINGERPRINT:
            return {"added": [], "removed": [], "modified": []}
        from orchestrator.catalog.models import CatalogSkill

        before = {
            str(r.skill_id): str(r.content_hash or "")
            for r in CatalogSkill.objects.all()
        }
        seen: set[str] = set()
        for root, mode in self._skill_disk_roots():
            seen.update(
                self._import_skill_root(
                    root, mode, keep_bindings=True, only_if_pristine=True
                )
            )
        after = {
            str(r.skill_id): str(r.content_hash or "")
            for r in CatalogSkill.objects.filter(skill_id__in=seen)
        }
        _SKILLS_FINGERPRINT = fingerprint
        added = sorted(sid for sid in seen if sid not in before)
        modified = sorted(
            sid for sid in seen if sid in before and before[sid] != after.get(sid, "")
        )
        if added or modified:
            logger.info("skills refreshed from disk: added=%s modified=%s", added, modified)
            _invalidate()
        return {"added": added, "removed": [], "modified": modified}

    def reference_hosts(self) -> frozenset[str]:
        """Hosts enabled MCP servers declare as reference infrastructure."""
        from orchestrator.catalog.models import CatalogMcp

        try:
            rows = CatalogMcp.objects.filter(enabled=True).values_list(
                "reference_hosts", flat=True
            )
            return frozenset(
                str(h).strip().lower()
                for hosts in rows
                for h in (hosts or ())
                if str(h).strip()
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("reference_hosts unavailable: %s", exc)
            return frozenset()

    def _import_mcp_packs(self) -> int:
        """Bootstrap ``mcps/`` into an empty catalog (first boot)."""
        from orchestrator.catalog.models import CatalogMcp

        try:
            if CatalogMcp.objects.exists():
                return 0
        except Exception as exc:
            logger.debug("mcp table unavailable: %s", exc)
            return 0
        result = self.sync_mcps_from_disk()
        added = len(result.get("added") or [])
        if added:
            logger.info("bootstrapped %s MCP servers into the platform catalog", added)
        return added

    def sync_mcps_from_disk(self) -> dict[str, list[str]]:
        """Re-read ``mcps/`` packs into the catalog. Preserves role bindings."""
        from orchestrator.catalog.models import CatalogMcp

        root = Path(get_config().mcps_dir)
        if not root.is_dir():
            return {"added": [], "removed": [], "modified": []}
        before = {
            str(row.mcp_id): str(row.content_hash or "")
            for row in CatalogMcp.objects.all()
        }
        from orchestrator.mcp.install import refresh_mcp_root

        ids = refresh_mcp_root(root, keep_bindings=True)
        after = {
            str(row.mcp_id): str(row.content_hash or "")
            for row in CatalogMcp.objects.filter(mcp_id__in=ids)
        }
        seen = set(ids)
        # Pack-backed rows (they carry files) whose folder is gone were deleted from disk: drop them too.
        # Document-only rows (``upsert_mcp``) have no files and are not disk-managed, so they stay.
        from orchestrator.catalog.models import CatalogFile

        packed = {str(r.owner_id) for r in CatalogFile.objects.filter(owner_kind=OWNER_MCP)}
        removed: list[str] = []
        for mid in sorted(set(before) - seen):
            if mid in packed and not (root / mid).is_dir():   # a folder that merely failed to parse is not "deleted"
                try:
                    self.delete_mcp(mid)
                    removed.append(mid)
                except KeyError:
                    pass
        added = sorted(mid for mid in seen if mid not in before)
        modified = sorted(
            mid
            for mid in seen
            if mid in before and before[mid] != after.get(mid, "")
        )
        return {"added": added, "removed": removed, "modified": modified}

    def _import_skill_root(
        self,
        root: Path,
        mode: str,
        *,
        keep_bindings: bool,
        only_if_pristine: bool = False,
    ) -> list[str]:
        from orchestrator.skills.providers.base import CLAUDE_PROVIDER, PEON_PROVIDER
        from orchestrator.skills.registry_scan import discover_skill_candidates

        imported: list[str] = []
        default = PEON_PROVIDER if mode == "infer" else CLAUDE_PROVIDER
        source = "native" if mode == "infer" else "imported"
        try:
            candidates = discover_skill_candidates(
                root, source=source, skip_helpers=True
            )
        except Exception as exc:
            logger.warning("skill bootstrap scan failed for %s: %s", root, exc)
            return []
        for candidate in candidates:
            default_provider = default if mode == "infer" else CLAUDE_PROVIDER
            try:
                rec = self.upsert_skill_from_candidate(
                    candidate,
                    default_provider=default_provider,
                    keep_bindings=keep_bindings,
                    only_if_pristine=only_if_pristine,
                )
            except Exception as exc:
                logger.warning("skipped skill %s: %s", candidate.root, exc)
                continue
            if rec is not None:
                imported.append(rec.skill_id)
        return imported

    def upsert_skill_from_candidate(
        self,
        candidate,
        *,
        default_provider: str,
        keep_bindings: bool = False,
        only_if_pristine: bool = False,
        from_disk: bool = True,
    ) -> "SkillRecord | None":
        """Upsert one ``SkillCandidate`` (from ``discover_skill_candidates``).

        Shared by the disk bootstrap/sync scan and the marketplace importer —
        the actual rules (namespace disambiguation, kind inference, file
        collection, affinity-derived bindings) live here exactly once.
        """
        from orchestrator.skills.affinity import read_affinity
        from orchestrator.skills.common import apply_skill_namespace, parse_frontmatter_yaml
        from orchestrator.skills.kind import catalog_id_for, infer_skill_kind

        text = candidate.manifest.read_text(encoding="utf-8")
        if candidate.namespace:
            # upsert_skill derives the catalog id from SKILL.md's own
            # frontmatter `name:`, not from a skill_id hint (a mismatched
            # hint is treated as a rename request, not an override) — so
            # disambiguating two bundles' same-named skill means rewriting
            # the frontmatter itself before upserting.
            text = apply_skill_namespace(text, candidate.namespace)
        parsed_fm = parse_frontmatter_yaml(text)
        frontmatter_name = (
            str(parsed_fm[0].get("name") or "").strip().lower() if parsed_fm else ""
        ) or candidate.root.name
        kind = infer_skill_kind(candidate.root, default_provider=default_provider)
        files: dict[str, str] = {}
        for path in sorted(candidate.root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(candidate.root).as_posix()
            if rel in {"SKILL.md", "peon.affinity.yaml"} or path.name.startswith("."):
                continue
            try:
                files[rel] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
        try:
            affinity = read_affinity(candidate.root)
        except Exception:
            affinity = None
        binding_rows = []
        if affinity is not None and not keep_bindings:
            for binding in affinity.bindings:
                if binding.score < _AFFINITY_THRESHOLD:
                    continue
                role = self.get_role(binding.role_id)
                if role is None:
                    continue
                binding_rows.append(
                    {"role_id": role.id, "score": binding.score, "lead": binding.lead}
                )
        return self.upsert_skill(
            skill_md=text,
            files=files,
            skill_id=catalog_id_for(frontmatter_name, kind),
            provider=kind.provider,
            bindings=binding_rows or None,
            keep_bindings=keep_bindings,
            from_disk=from_disk,
            only_if_pristine=only_if_pristine,
        )

    def ingest_imported_skill(self, name: str) -> str:
        """Load a pack from ``skills/<name>`` into the platform catalog.

        A thin wrapper over ``upsert_skill_from_candidate`` — file collection,
        affinity-derived bindings, and kind inference all live there once.
        """
        from orchestrator.skills.common import find_manifest
        from orchestrator.skills.providers.base import CLAUDE_PROVIDER
        from orchestrator.skills.registry_scan import SkillCandidate

        slug = str(name or "").strip().lower()
        if not slug:
            raise ValueError("skill name is required")
        cfg = get_config()
        skills_root = Path(cfg.skills_dir)
        legacy = Path(cfg.skills_imported_dir or cfg.skills_dir)
        root = skills_root / slug
        if not root.is_dir() and legacy.resolve() != skills_root.resolve():
            root = legacy / slug
        if not root.is_dir():
            raise ValueError(f"skill directory missing: {skills_root / slug}")
        manifest = find_manifest(root)
        if manifest is None:
            raise ValueError(f"{root} has no SKILL.md")
        candidate = SkillCandidate(root=root, manifest=manifest, source="imported")
        # Installed / Lab packs default to claude unless signals say otherwise.
        rec = self.upsert_skill_from_candidate(candidate, default_provider=CLAUDE_PROVIDER)
        return rec.skill_id


def _manifest_payload(manifest) -> dict[str, Any]:
    return {
        "tools": list(manifest.allowed_tools),
        "allow_binaries": list(manifest.allow_binaries),
        "tags": list(manifest.tags),
        "compatibility": manifest.compatibility,
        "license": manifest.license,
        "when_to_use": manifest.when_to_use,
        "argument_hint": manifest.argument_hint,
        "arguments": list(manifest.arguments),
        "disallowed_tools": list(manifest.disallowed_tools),
        "disable_model_invocation": manifest.disable_model_invocation,
        "user_invocable": manifest.user_invocable,
        "model": manifest.model,
        "context": manifest.context,
        "agent": manifest.agent,
        "background": manifest.background,
        "paths": list(manifest.paths),
        "shell": manifest.shell,
        "hooks": manifest.hooks,
        "profile": manifest.profile,
    }


def _setup_post(metadata: dict[str, str] | None) -> list[str]:
    """Commands from SKILL.md ``metadata: {setup-post: ...}`` (JSON list or lines)."""
    raw = str((metadata or {}).get("setup-post") or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = []
        items = [str(x) for x in parsed] if isinstance(parsed, list) else []
    else:
        items = raw.splitlines()
    return [c.strip() for c in items if c.strip() and "\x00" not in c][:8]


def _setup_from_files(files: dict[str, str]) -> dict[str, str] | None:
    for kind, name in (
        ("pip", "requirements.txt"),
        ("pip", "pyproject.toml"),
        ("npm", "package.json"),
    ):
        if name in files:
            return {"kind": kind, "cwd": ".", "manifest": name}
    return None


_ENV_TYPES = frozenset({"string", "secret", "boolean", "number", "choice"})
_SECRET_HINT = re.compile(
    r"(token|password|secret|api_?key|authorization|access_token|refresh_token)", re.I
)


def _mcp_from_row(row, bindings: tuple[Binding, ...], files: dict[str, str]) -> McpRecord:
    schema_raw = row.env_schema or []
    schema: list[tuple[str, bool]] = []
    schema_full: list[dict[str, Any]] = []
    if isinstance(schema_raw, list):
        for item in schema_raw:
            if isinstance(item, str) and item.strip():
                name, required, extra = item.strip(), True, {}
            elif isinstance(item, dict) and str(item.get("name") or "").strip():
                name = str(item["name"]).strip()
                required = bool(item.get("required", True))
                extra = item
            else:
                continue
            schema.append((name, required))
            etype = str(extra.get("type") or "").strip().lower()
            if etype not in _ENV_TYPES:
                etype = "secret" if _SECRET_HINT.search(name) else "string"
            schema_full.append(
                {
                    "name": name,
                    "required": required,
                    "type": etype,
                    "description": str(extra.get("description") or ""),
                    "default": "" if extra.get("default") is None else str(extra.get("default")),
                    "choices": [str(c) for c in (extra.get("choices") or [])],
                }
            )
    install = dict(row.install) if isinstance(row.install, dict) else None
    args = tuple(str(a) for a in (row.args or []))
    return McpRecord(
        mcp_id=str(row.mcp_id),
        description=str(row.description or ""),
        transport=str(row.transport or "stdio"),
        command=str(row.command or ""),
        args=args,
        cwd=str(getattr(row, "cwd", "") or ""),
        install=install,
        env_schema=tuple(schema),
        env_schema_full=tuple(schema_full),
        enabled=bool(row.enabled),
        files=files,
        bindings=bindings,
        reference_hosts=tuple(
            str(h) for h in (getattr(row, "reference_hosts", None) or ())
        ),
    )
