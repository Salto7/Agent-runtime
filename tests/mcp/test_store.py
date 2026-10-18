from orchestrator.mcp.store import InMemoryMcpStore, McpRecord


def test_upsert_and_get_roundtrip():
    store = InMemoryMcpStore()
    store.upsert_mcp(McpRecord(mcp_id="x", command="python", args=("a.py",)))
    rec = store.get_mcp("x")
    assert rec is not None
    assert rec.command == "python"
    assert rec.args == ("a.py",)


def test_list_mcps_sorted_by_id():
    store = InMemoryMcpStore()
    store.upsert_mcp(McpRecord(mcp_id="b"))
    store.upsert_mcp(McpRecord(mcp_id="a"))
    assert [r.mcp_id for r in store.list_mcps()] == ["a", "b"]


def test_delete_mcp_removes_it():
    store = InMemoryMcpStore()
    store.upsert_mcp(McpRecord(mcp_id="x"))
    store.delete_mcp("x")
    assert store.get_mcp("x") is None


def test_env_schema_property_derives_plain_pairs():
    rec = McpRecord(
        mcp_id="x",
        env_schema_full=(
            {"name": "A", "required": True, "type": "secret"},
            {"name": "B", "required": False, "type": "string"},
        ),
    )
    assert rec.env_schema == (("A", True), ("B", False))
