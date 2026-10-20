"""Capability bodies. Each one delegates to ``SandboxTools``."""

from __future__ import annotations

from orchestrator.skills.runtime.sandbox import (
    SandboxTools,
    emit,
    emit_event,
    require_bound,
)

__all__ = [
    "emit",
    "emit_event",
    "provision_cli_body",
    "require_bound",
    "bash_body",
    "run_periodic_body",
    "sandbox_status_body",
    "sandbox_status_text",
]


def sandbox_status_body() -> str:
    return SandboxTools().status()


def sandbox_status_text(*, extra_binaries: bool = False) -> str:
    return SandboxTools().status(extra_binaries=extra_binaries)


def provision_cli_body(
    binary: str,
    package: str = "",
    *,
    enforce_role_allowlist: bool = False,
) -> str:
    return SandboxTools().provision(
        binary,
        package=package,
        enforce_role_allowlist=enforce_role_allowlist,
    )


def bash_body(command: str) -> str:
    """Claude Bash tool body."""
    return SandboxTools().bash(command)



def run_periodic_body(
    command: str,
    interval_seconds: int = 30,
    duration_seconds: int = 120,
    package: str = "",
) -> str:
    return SandboxTools().periodic(
        command,
        interval_seconds=interval_seconds,
        duration_seconds=duration_seconds,
        package=package,
    )
