import json
import subprocess

import pytest

from orchestrator.mcp.install import McpInstaller, _inferred_mcp_id, _repo_name_from_origin
from orchestrator.mcp.store import InMemoryMcpStore


def test_installs_from_disk_manifest(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "mcp.json").write_text(
        json.dumps({"name": "disk-server", "command": "python", "args": ["server.py"]})
    )
    store = InMemoryMcpStore()
    installer = McpInstaller(store=store)
    ids = installer.install_source("disk", str(pack))
    assert ids == ["disk-server"]
    rec = store.get_mcp("disk-server")
    assert rec is not None
    assert rec.command == "python"


def test_installs_from_npx():
    store = InMemoryMcpStore()
    installer = McpInstaller(store=store)
    [mcp_id] = installer.install_source("npx", "@scope/my-tool")
    rec = store.get_mcp(mcp_id)
    assert rec.command == "npx"
    assert rec.args == ("-y", "@scope/my-tool")


def test_infers_stdio_server_from_main_py(tmp_path):
    """Regression: bloodhound-mcp-style packs use main.py, not mcp_server.py."""
    pack = tmp_path / "bloodhound_mcp"
    pack.mkdir()
    (pack / "main.py").write_text("print('hi')\n")
    (pack / "pyproject.toml").write_text("[project]\nname='x'\n")
    store = InMemoryMcpStore()
    installer = McpInstaller(store=store)
    ids = installer.install_source("disk", str(pack))
    assert ids == ["bloodhound-mcp"]
    rec = store.get_mcp("bloodhound-mcp")
    assert rec.command == "python"
    assert rec.args == ("main.py",)
    assert rec.install == {"kind": "uv", "manifest": "pyproject.toml"}


def test_inferred_mcp_id_falls_back_to_origin_for_generic_clone_dir(tmp_path):
    """A git clone always lands at .../repo — the real name must come from the URL."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("")
    mcp_id = _inferred_mcp_id(root, "main.py", origin="git:https://github.com/owner/bloodhound_mcp.git")
    assert mcp_id == "bloodhound-mcp"


def test_repo_name_from_origin_strips_git_prefix_and_suffix():
    assert _repo_name_from_origin("git:https://github.com/owner/my-repo.git") == "my-repo"
    assert _repo_name_from_origin("") == ""


def test_directory_with_no_manifest_raises():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        empty = Path(tmp) / "empty"
        empty.mkdir()
        installer = McpInstaller(store=InMemoryMcpStore())
        with pytest.raises(ValueError, match="no mcp.json"):
            installer.install_source("disk", str(empty))


def test_mcps_dir_persists_pack_to_disk(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "mcp.json").write_text(json.dumps({"name": "persisted", "command": "python"}))
    mcps_dir = tmp_path / "mcps"
    installer = McpInstaller(store=InMemoryMcpStore(), mcps_dir=mcps_dir)
    installer.install_source("disk", str(pack))
    assert (mcps_dir / "persisted" / "mcp.json").is_file()


def test_refresh_root_reimports_every_persisted_pack(tmp_path):
    mcps_dir = tmp_path / "mcps"
    mcps_dir.mkdir()
    one = mcps_dir / "one"
    one.mkdir()
    (one / "mcp.json").write_text(json.dumps({"name": "one", "command": "python"}))
    two = mcps_dir / "two"
    two.mkdir()
    (two / "mcp.json").write_text(json.dumps({"name": "two", "command": "node"}))
    store = InMemoryMcpStore()
    installer = McpInstaller(store=store, mcps_dir=mcps_dir)
    ids = installer.refresh_root(mcps_dir)
    assert sorted(ids) == ["one", "two"]
    assert store.get_mcp("one") is not None
    assert store.get_mcp("two") is not None


def test_install_relative_files_drops_hidden_env_and_keeps_mcp_json():
    store = InMemoryMcpStore()
    installer = McpInstaller(store=store)
    files = [
        (".env", b"SECRET=1"),
        ("mcp.json", json.dumps({"name": "dropped", "command": "python"}).encode()),
    ]
    ids = installer.install_relative_files(files)
    assert ids == ["dropped"]
    assert store.get_mcp("dropped") is not None


@pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git not installed",
)
def test_installs_from_git_clone(tmp_path):
    """Real git clone, local filesystem remote — no network access."""
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    work.mkdir()
    (work / "mcp.json").write_text(json.dumps({"name": "cloned", "command": "python"}))
    subprocess.run(["git", "init", "-q", str(remote), "--bare"], check=True)
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(work), "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "x"],
        check=True,
    )
    branch = subprocess.run(
        ["git", "-C", str(work), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", f"HEAD:{branch}"], check=True)
    subprocess.run(
        ["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", f"refs/heads/{branch}"],
        check=True,
    )

    store = InMemoryMcpStore()
    installer = McpInstaller(store=store)
    ids = installer.install_source("git", str(remote))
    assert ids == ["cloned"]
