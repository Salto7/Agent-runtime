"""Resolve missing CLI installs: Kali PATH → tools/catalog → LLM."""

from __future__ import annotations

import logging
from typing import Any

from agent_runtime.api import Session
from orchestrator.prompts import INSTALL_CASCADE, PLANNER_INSTALL_SYSTEM
from orchestrator.tools.catalog import CatalogProvisioner, ToolCatalog
from orchestrator.tools.install_step_base import InstallStepBase
from orchestrator.tools.install_steps import AptInstallStep
from orchestrator.utils.llm import chat_json, llm_config
from orchestrator.utils.service import SharedServiceBase

logger = logging.getLogger(__name__)


def cli_on_path(binary: str) -> bool:
    """True when the bound sandbox has ``binary`` on PATH (Kali / prior install)."""
    name = (binary or "").strip()
    return bool(name and Session.current().which(name))


class InstallResolver(SharedServiceBase):
    """Provision until the binary is on PATH.

    Order: (1) already on Kali PATH → (2) ``tools/catalog`` → (3) LLM.
    """

    def resolve(
        self,
        binary: str,
        *,
        package: str = "",
    ) -> tuple[bool, str]:
        name = (binary or "").strip()
        if not name:
            return True, "no binary to provision"

        # 1) Already present in the Kali sandbox (or prior provision).
        if cli_on_path(name):
            return True, f"{name} already installed (Kali PATH)"

        errors: list[str] = []

        # 2) tools/catalog registry.
        cat = self._from_catalog(name)
        if cat is not None:
            if cat[0]:
                return cat
            errors.append(cat[1])

        # Optional explicit apt package from the caller (still before LLM).
        pkg = (package or "").strip()
        if pkg:
            ok, msg = self._apt([pkg], binary=name)
            if ok:
                return True, msg
            errors.append(msg)

        # 3) LLM planner — last resort.
        plan = self._from_planner(name, prior_error="; ".join(errors))
        if plan is not None:
            return plan

        hint = f"add tools/catalog YAML or set an LLM key ({INSTALL_CASCADE})"
        detail = "; ".join(errors) or f"no install recipe for {name!r}"
        return False, f"{detail}; {hint}"

    @staticmethod
    def _from_catalog(binary: str) -> tuple[bool, str] | None:
        try:
            if ToolCatalog.shared().by_binary(binary) is None:
                return None
            return CatalogProvisioner.shared().provision_binary(binary)
        except Exception as exc:
            logger.debug("catalog resolve failed for %s: %s", binary, exc)
            return None

    def _from_planner(
        self, binary: str, *, prior_error: str
    ) -> tuple[bool, str] | None:
        try:
            if not llm_config().api_key:
                return None
            from orchestrator.utils import llm_usage

            with llm_usage.purpose("install_resolver"):
                payload = chat_json(
                    PLANNER_INSTALL_SYSTEM,
                    f"binary={binary!r} prior_error={prior_error!r}",
                )
        except Exception as exc:
            logger.info("planner install resolve failed for %s: %s", binary, exc)
            return None
        if not isinstance(payload, dict):
            return None
        steps = list(payload.get("install") or [])
        if not steps:
            return None
        try:
            return self._apply_steps(steps, binary=binary, source="planner")
        except Exception as exc:  # noqa: BLE001 — a bad LLM recipe must come back as a failure, not an exception
            return False, f"planner: install step failed: {exc}"

    def _apply_steps(
        self, steps: list[dict[str, Any]], *, binary: str, source: str
    ) -> tuple[bool, str]:
        for raw in steps:
            if not isinstance(raw, dict):
                continue
            step = InstallStepBase.from_dict(raw)
            if step is None:
                continue
            ok, msg = step.apply(binary=binary)
            if ok and cli_on_path(binary):
                return True, f"{binary} installed via {source}"
            if not ok:
                return False, f"{source}: {msg}"
        if cli_on_path(binary):
            return True, f"{binary} installed via {source}"
        return False, f"{source}: steps ran but {binary!r} still missing"

    def _apt(self, packages: list[str], *, binary: str) -> tuple[bool, str]:
        step = AptInstallStep({"packages": packages})
        ok, msg = step.apply(binary=binary)
        if ok and cli_on_path(binary):
            return True, f"{binary} installed via apt"
        return False, msg or f"apt failed for {binary}"

