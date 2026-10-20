"""Capability callables for authoring / command helpers.

Importing this module registers tools via ``@capability`` decorators.
Call ``orchestrator.capabilities.ensure_registered()`` so registration runs
even when this module was not imported yet.

These names wrap ``SandboxTools``. Crew tools call that module directly.
"""

from __future__ import annotations

from orchestrator.capabilities.bodies import (
    bash_body,
    provision_cli_body,
    run_periodic_body,
    sandbox_status_text,
)
from orchestrator.capabilities.engagement import EngagementTools
from orchestrator.capabilities.registry import CapabilityGroup, capability

__all__ = [
    "ask_operator",
    "list_findings",
    "list_objectives",
    "bash",
    "provision_cli",
    "provision_cli_body",
    "record_finding",
    "check_inbox",
    "forget_memory",
    "go_idle",
    "list_team",
    "record_assets",
    "record_findings",
    "recall_memory",
    "run_periodic",
    "save_memory",
    "send_message",
    "sandbox_status",
    "sandbox_status_text",
    "update_objective_status",
]


@capability(CapabilityGroup.SANDBOX)
def sandbox_status() -> str:
    """Show bound sandbox mode and name."""
    return sandbox_status_text(extra_binaries=True)


@capability(CapabilityGroup.SANDBOX)
def provision_cli(binary: str, package: str = "") -> str:
    """Install/verify a CLI on PATH (tools/catalog → LLM). Prefer over apt/curl via bash."""
    return provision_cli_body(
        binary, package=package, enforce_role_allowlist=True
    )


@capability(CapabilityGroup.SANDBOX, tags={"shell"})
def bash(command: str) -> str:
    """Claude Bash tool. Run a shell command in the job sandbox (RoE applies)."""
    return bash_body(command)


@capability(CapabilityGroup.SANDBOX, tags={"watchdog", "periodic"})
def run_periodic(
    command: str,
    interval_seconds: int = 30,
    duration_seconds: int = 120,
    package: str = "",
) -> str:
    """Watchdog ticks only — repeat a sandbox shell command on an interval."""
    return run_periodic_body(
        command,
        interval_seconds=interval_seconds,
        duration_seconds=duration_seconds,
        package=package,
    )


@capability(CapabilityGroup.ENGAGEMENT)
def list_objectives() -> str:
    """List host-defined objectives for the current session (via ports)."""
    return EngagementTools.list_objectives()


@capability(CapabilityGroup.ENGAGEMENT)
def update_objective_status(seq: int, status: str, note: str = "") -> str:
    """Update an objective status via host ports."""
    return EngagementTools.update_objective_status(int(seq), status, note)


@capability(CapabilityGroup.ENGAGEMENT)
def ask_operator(
    question: str, options: str = "", blocks_job: bool = True
) -> str:
    """Ask the human operator (HITL). Prefer when stuck or decision needed."""
    return EngagementTools.ask_operator(
        question,
        options=options or "",
        blocks_job=bool(blocks_job),
    )


@capability(CapabilityGroup.ENGAGEMENT)
def record_finding(
    title: str,
    severity: str = "info",
    kind: str = "observation",
    evidence: str = "",
    host: str = "",
    port: int = 0,
    protocol: str = "",
    description: str = "",
    asset_type: str = "",
    evidence_path: str = "",
    remediation: str = "",
) -> str:
    """Record one engagement finding about a subject — not run/objective status."""
    return EngagementTools.record_finding(
        title=title,
        severity=severity,
        kind=kind,
        evidence=evidence,
        host=host,
        port=port or None,
        protocol=protocol,
        description=description,
        asset_type=asset_type,
        evidence_path=evidence_path,
        remediation=remediation,
    )


@capability(CapabilityGroup.ENGAGEMENT)
def record_findings(findings_json: str) -> str:
    """Record multiple engagement findings from a JSON list (not status updates)."""
    return EngagementTools.record_findings(findings_json)


@capability(CapabilityGroup.ENGAGEMENT)
def recall_memory(query: str) -> str:
    """Search persistent memory for this engagement and platform lessons."""
    return EngagementTools.recall_memory(query)


@capability(CapabilityGroup.ENGAGEMENT)
def save_memory(
    name: str, kind: str, description: str, body: str = "", scope: str = "project"
) -> str:
    """Save a durable memory (lesson | feedback | fact | decision | reference)."""
    return EngagementTools.save_memory(name, kind, description, body, scope or "project")


@capability(CapabilityGroup.ENGAGEMENT)
def forget_memory(name: str, scope: str = "project") -> str:
    """Retire a stale memory entry."""
    return EngagementTools.forget_memory(name, scope or "project")


@capability(CapabilityGroup.ENGAGEMENT)
def list_team() -> str:
    """List agents on this project (id, objective, role, A2A state)."""
    return EngagementTools.list_team()


@capability(CapabilityGroup.ENGAGEMENT)
def send_message(to: str, content: str, kind: str = "inform") -> str:
    """Send an A2A message to another agent (wakes it if idle)."""
    return EngagementTools.send_message(to, content, kind=kind or "inform")


@capability(CapabilityGroup.ENGAGEMENT)
def check_inbox() -> str:
    """Read and consume messages from other agents or the operator."""
    return EngagementTools.check_inbox()


@capability(CapabilityGroup.ENGAGEMENT)
def go_idle(reason: str = "") -> str:
    """Park this agent until it receives a message."""
    return EngagementTools.go_idle(reason or "")


@capability(CapabilityGroup.ENGAGEMENT)
def record_assets(assets_json: str, relations_json: str = "") -> str:
    """Add discovered assets and typed relations to the engagement asset graph."""
    return EngagementTools.record_assets(assets_json or "[]", relations_json or "[]")


@capability(CapabilityGroup.ENGAGEMENT)
def list_findings(kind: str = "") -> str:
    """List engagement findings already recorded (optional kind slug filter)."""
    return EngagementTools.list_findings(kind=kind)
