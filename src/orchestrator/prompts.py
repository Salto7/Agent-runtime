"""Shared LLM prompt fragments (install cascade, agent recover, planner)."""

from __future__ import annotations

# CLI provision order (runtime + authoring + agent preamble).
# 1) Binary already on PATH (Kali image / prior install)
# 2) tools/catalog recipe
# 3) LLM planner recipe
INSTALL_CASCADE = "Kali PATH → tools/catalog → LLM"

INSTALL_MISSING_HINT = (
    f"If missing: {INSTALL_CASCADE} via provision_cli."
)

# Last-resort LLM install recipe (aligned with tools-suggestor / skill-writer).
# Steps in ``install`` run in list order — put apt deps before git_clone / pip.
PLANNER_INSTALL_SYSTEM = (
    "Install a missing CLI in a Kali Linux Docker sandbox (kalilinux/kali-rolling "
    "based). Many pentest CLIs are already packaged — prefer apt from Kali repos. "
    "Reply with JSON only, no prose: "
    '{"install":[{"type":"apt"|"github_release"|"git_clone"|"pip"|"custom",...}],'
    '"verify":[{"command":"..."}]}. '
    "List install steps in execution order. Prefer: apt when the tool is a Kali/"
    "Debian package; github_release for release binaries; apt then git_clone for "
    "cloned repos (git_clone needs repo, entrypoint, binary). For git_clone, ensure "
    "the interpreter in the entrypoint shebang exists (apt dependency / alias). "
    "Use custom only as a last resort — inline shell must apt-get prerequisites "
    "itself. verify must succeed after all steps; use the binary name on PATH."
)

# Catalog recipe preference when drafting/revising tools/catalog YAML.
CATALOG_INSTALL_PREFER = (
    "Prefer apt when the package exists on Kali (kali-rolling); else github_release; "
    "else apt then git_clone; else pip; use custom only as a last resort "
    "(custom scripts must apt-get their own prerequisites)."
)

# Engagement findings vs run/objective status (agent preamble + tools).
FINDINGS_GUIDANCE = (
    "Findings are engagement discoveries about any in-scope subject "
    "(network assets, files, malware/samples, source, packages, identities, "
    "cloud resources, or other producer-labeled assets), each with evidence. "
    "kind and asset_type are free-form slugs — not a fixed taxonomy. "
    "Do NOT record findings for job/objective/agent progress or role run completion; "
    "use update_objective_status for objectives and stream/logs for run status."
)

