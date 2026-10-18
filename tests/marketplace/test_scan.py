import json

from orchestrator.marketplace.scan import scan_agents, scan_all, scan_mcp_pointers, scan_skills


def test_scan_skills_finds_nested_skill_md(tmp_path):
    pack = tmp_path / "skills" / "recon"
    pack.mkdir(parents=True)
    (pack / "SKILL.md").write_text(
        "---\nname: recon\ndescription: do recon\n---\nBody text\n"
    )
    results = scan_skills(tmp_path)
    assert results == [{"name": "recon", "description": "do recon", "path": "skills/recon"}]


def test_scan_skills_skips_hidden_and_junk_dirs(tmp_path):
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "SKILL.md").write_text("---\nname: x\n---\n")
    (tmp_path / ".git" / "sub").mkdir(parents=True)
    (tmp_path / ".git" / "sub" / "SKILL.md").write_text("---\nname: y\n---\n")
    assert scan_skills(tmp_path) == []


def test_scan_skills_falls_back_to_dir_name_without_frontmatter_name(tmp_path):
    pack = tmp_path / "weird"
    pack.mkdir()
    (pack / "SKILL.md").write_text("no frontmatter here")
    [entry] = scan_skills(tmp_path)
    assert entry["name"] == "weird"
    assert entry["description"] == ""


def test_scan_agents_only_matches_files_under_agents_dir(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "reviewer.toml").write_text(
        'name = "reviewer"\ndescription = "reviews code"\n'
    )
    (tmp_path / "notes.toml").write_text('name = "ignored"\ndescription = "x"\n')
    results = scan_agents(tmp_path)
    assert [r["name"] for r in results] == ["reviewer"]


def test_scan_mcp_pointers_skips_manifests_with_a_command(tmp_path):
    runnable = tmp_path / "runnable"
    runnable.mkdir()
    (runnable / "mcp.json").write_text(json.dumps({"name": "runnable", "command": "python"}))

    pointer = tmp_path / "pointer"
    pointer.mkdir()
    (pointer / "mcp.json").write_text(
        json.dumps({"name": "pointer", "repo": "https://github.com/owner/repo"})
    )

    results = scan_mcp_pointers(tmp_path)
    assert [r["name"] for r in results] == ["pointer"]
    assert results[0]["repo_url"] == "https://github.com/owner/repo"


def test_scan_all_aggregates_all_three_kinds(tmp_path):
    (tmp_path / "skills" / "a").mkdir(parents=True)
    (tmp_path / "skills" / "a" / "SKILL.md").write_text("---\nname: a\n---\n")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "b.toml").write_text('name = "b"\ndescription = "d"\n')
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "mcp.json").write_text(json.dumps({"name": "c", "repo": "x"}))

    result = scan_all(tmp_path)
    assert {r["name"] for r in result["skills"]} == {"a"}
    assert {r["name"] for r in result["agents"]} == {"b"}
    assert {r["name"] for r in result["mcp_pointers"]} == {"c"}
