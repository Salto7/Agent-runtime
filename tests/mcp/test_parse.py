import pytest

from orchestrator.mcp.parse import McpParseError, parse_mcp_document


def test_parses_single_server_object():
    (spec,) = parse_mcp_document(
        {"name": "my-server", "command": "python", "args": ["server.py"]}
    )
    assert spec.mcp_id == "my-server"
    assert spec.command == "python"
    assert spec.args == ("server.py",)
    assert spec.transport == "stdio"


def test_parses_mcpservers_map_into_multiple_specs():
    specs = parse_mcp_document(
        {
            "mcpServers": {
                "a": {"command": "python", "args": ["a.py"]},
                "b": {"command": "node", "args": ["b.js"]},
            }
        }
    )
    assert sorted(s.mcp_id for s in specs) == ["a", "b"]


def test_rejects_non_stdio_transport():
    with pytest.raises(McpParseError):
        parse_mcp_document({"name": "x", "transport": "sse", "command": "python"})


def test_rejects_absolute_command_path():
    with pytest.raises(McpParseError):
        parse_mcp_document({"name": "x", "command": "/usr/bin/python"})


def test_rejects_secret_values_in_env():
    with pytest.raises(McpParseError):
        parse_mcp_document(
            {
                "name": "x",
                "command": "python",
                "env": {"API_KEY": "sk-abc123"},
            }
        )


def test_env_schema_infers_secret_type_from_name():
    (spec,) = parse_mcp_document(
        {"name": "x", "command": "python", "env_schema": ["API_TOKEN", "DEBUG"]}
    )
    by_name = {f["name"]: f for f in spec.env_schema_full}
    assert by_name["API_TOKEN"]["type"] == "secret"
    assert by_name["DEBUG"]["type"] == "string"
