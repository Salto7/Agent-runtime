"""Scan an external marketplace source (git repo or uploaded zip) for
skills, agent definitions, and MCP-server pointers — staged for human review
before anything is written to a live catalog.

Read-only: nothing from a scanned source is executed, imported, or
shell-invoked. ``fetch`` gets the source onto disk; ``scan`` reads it.
"""

from __future__ import annotations

from orchestrator.marketplace.fetch import fetch_git, fetch_zip_archive, scan_root, source_dir
from orchestrator.marketplace.scan import scan_agents, scan_all, scan_mcp_pointers, scan_skills

__all__ = [
    "fetch_git",
    "fetch_zip_archive",
    "scan_agents",
    "scan_all",
    "scan_mcp_pointers",
    "scan_root",
    "scan_skills",
    "source_dir",
]
