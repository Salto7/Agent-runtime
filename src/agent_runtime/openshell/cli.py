"""OpenShell CLI. Sandbox create / exec / delete only."""

from __future__ import annotations

from agent_runtime.cli_base import CLIBase

_DEFAULT_POLICY = """version: 1
filesystem_policy:
  include_workdir: true
  read_only: [/usr, /lib, /lib64, /etc]
  read_write: [/tmp]
landlock:
  compatibility: best_effort
network_policies: {}
"""


def policy_text() -> str:
    """Default-deny network. The operator warning on the create form matches this."""
    return _DEFAULT_POLICY


class OpenShellCli(CLIBase):
    binary_name = "openshell"
    missing_error = (
        "openshell CLI missing — install OpenShell or create the project "
        "with the Sandbox runtime"
    )
