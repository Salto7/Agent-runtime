"""MCP document parsing and in-sandbox provisioning.

Heavy runtime helpers live in ``orchestrator.mcp.provision`` and are imported
at call sites so catalog/parse paths stay light.
"""

from orchestrator.mcp.parse import McpParseError, McpSpec, parse_mcp_document

__all__ = ["McpParseError", "McpSpec", "parse_mcp_document"]
