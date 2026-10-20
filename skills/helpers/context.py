"""Runtime context for a skill process (env, scope, command, identity)."""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Callable


# Soft preference for network-oriented skills — not a closed taxonomy.
_NETWORK_HINT = frozenset({"domain", "url", "ip", "host", "fqdn", "netblock", "cidr"})


def _coerce_target_list(raw) -> list[dict]:
    """Local coerce — skills must not import the orchestrator. Type is hint; one row per value."""
    by_value: dict[str, dict] = {}
    order: list[str] = []
    if not isinstance(raw, list):
        return []
    for item in raw:
        if isinstance(item, dict):
            value = str(item.get("value") or "").strip()
            typ = str(item.get("type") or "other").strip().lower() or "other"
        else:
            value = str(item or "").strip()
            typ = "other"
        if not value:
            continue
        key = value.lower()
        row = {"type": typ, "value": value}
        prev = by_value.get(key)
        if prev is None:
            by_value[key] = row
            order.append(key)
            continue
        # Prefer explicit producer type over bare other.
        if prev.get("type") in {"", "other"} and typ not in {"", "other"}:
            by_value[key] = row
    return [by_value[k] for k in order]


def _env_json_list(key: str) -> list[dict]:
    try:
        raw = json.loads(os.environ.get(key) or "[]")
    except json.JSONDecodeError:
        raw = []
    return _coerce_target_list(raw)


class SkillContext:
    """Reads orchestrator-injected environment for the current skill process."""

    def skill_name(self) -> str:
        return (os.environ.get("ORCHESTRATOR_SKILL_NAME") or "").strip()

    def job_id(self) -> str:
        return (
            os.environ.get("ORCHESTRATOR_JOB_ID", "").strip()
            or os.environ.get("ORCHESTRATOR_TASK_ID", "").strip()
        )

    def in_scope_assets(self) -> list[dict]:
        return _env_json_list("ORCHESTRATOR_IN_SCOPE")

    def exclusion_assets(self) -> list[dict]:
        return _env_json_list("ORCHESTRATOR_EXCLUSIONS")

    def seed_assets(self) -> list[dict]:
        return _env_json_list("ORCHESTRATOR_SEED")

    def network_targets(self) -> list[str]:
        """Prefer network-typed hints; otherwise all in-scope values."""
        assets = self.in_scope_assets()
        preferred = [t["value"] for t in assets if t.get("type") in _NETWORK_HINT]
        return preferred or [t["value"] for t in assets]

    def explicit_command(self) -> str:
        if len(sys.argv) > 1:
            # shlex.join keeps quoted arguments that a space-join would split.
            return shlex.join(sys.argv[1:]).strip()
        return ""

    def resolve_command(
        self,
        *,
        default_for_target: Callable[[str], str] | None = None,
    ) -> str:
        command = self.explicit_command()
        if command:
            return command
        if default_for_target is None:
            return ""
        targets = self.network_targets()
        return default_for_target(targets[0]) if targets else ""

    def resolve_commands(
        self,
        *,
        default_for_target: Callable[[str], str] | None = None,
    ) -> list[str]:
        command = self.explicit_command()
        if command:
            return [command]
        if default_for_target is None:
            return []
        return [default_for_target(t) for t in self.network_targets()]
