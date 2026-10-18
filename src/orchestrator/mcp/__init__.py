"""MCP (Model Context Protocol) server catalog: parse, store, and install
stdio MCP servers from disk, git, or npx.

Storage-agnostic, same pattern as ``orchestrator.brain``: logic lives in
``parse`` and ``install``, talking only to the ``McpStore`` protocol in
``store``. ``store`` ships an in-memory reference implementation; a host
app swaps in its own.

Installs never execute the installed server — only parsing, validation,
and (optionally) copying files to disk / a store happen here.
"""

from __future__ import annotations

from orchestrator.mcp.install import McpInstaller
from orchestrator.mcp.parse import McpParseError, McpSpec, parse_mcp_document
from orchestrator.mcp.store import InMemoryMcpStore, McpRecord, McpStore

__all__ = [
    "InMemoryMcpStore",
    "McpInstaller",
    "McpParseError",
    "McpRecord",
    "McpSpec",
    "McpStore",
    "parse_mcp_document",
]
