"""Shared path skip rules for skill / MCP disk installs.

Junk directory names are shared. MCP installs also skip every hidden
path segment (``.*``), so ``.env`` / ``.mcp.json`` are never copied into
``mcps/`` or stored as catalog files. Manifest discovery may still *read*
``.mcp.json`` as a source document.
"""

from __future__ import annotations

from pathlib import Path

# Non-hidden build / VCS / tooling dirs (and common dotted junk names).
JUNK_DIR_NAMES = frozenset(
    {
        "node_modules",
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "htmlcov",
    }
)


def is_hidden_name(name: str) -> bool:
    text = str(name or "")
    return text.startswith(".") and text not in {".", ".."}


def is_junk_dir_name(name: str) -> bool:
    return str(name or "") in JUNK_DIR_NAMES


def skip_dir_name(name: str, *, skip_hidden: bool = False) -> bool:
    """Whether a directory name should be skipped while walking a pack tree."""
    text = str(name or "")
    if is_junk_dir_name(text):
        return True
    return bool(skip_hidden and is_hidden_name(text))


def skip_file_name(name: str, *, skip_hidden: bool = False) -> bool:
    """Whether a file name should be omitted from installed pack payloads."""
    text = str(name or "")
    if text.endswith((".pyc", ".pyo")):
        return True
    return bool(skip_hidden and is_hidden_name(text))


def skip_rel_path(rel: str, *, skip_hidden: bool = False) -> bool:
    """Skip a relative pack path (``a/b/c``) based on any path segment."""
    text = str(rel or "").replace("\\", "/").strip("/")
    if not text or ".." in text.split("/"):
        return True
    for part in text.split("/"):
        if not part or part in {".", ".."}:
            return True
        if is_junk_dir_name(part):
            return True
        if skip_hidden and is_hidden_name(part):
            return True
    return False


def skip_path(path: Path, *, root: Path | None = None, skip_hidden: bool = False) -> bool:
    """Skip an on-disk path, optionally relative to ``root``."""
    target = Path(path)
    if root is not None:
        try:
            rel = target.relative_to(Path(root)).as_posix()
        except ValueError:
            rel = target.name
        return skip_rel_path(rel, skip_hidden=skip_hidden)
    return skip_rel_path(target.as_posix(), skip_hidden=skip_hidden)


def copytree_ignore(*, skip_hidden: bool = False):
    """``shutil.copytree`` ignore callable using the shared rules."""

    def _ignore(directory: str, names: list[str]) -> set[str]:
        skipped: set[str] = set()
        base = Path(directory)
        for name in names:
            child = base / name
            if child.is_symlink():   # a link in a pack could point at host files (~/.ssh…): never copy or read it
                skipped.add(name)
                continue
            if child.is_dir() or name in JUNK_DIR_NAMES:
                if skip_dir_name(name, skip_hidden=skip_hidden):
                    skipped.add(name)
                    continue
            if skip_file_name(name, skip_hidden=skip_hidden):
                skipped.add(name)
        return skipped

    return _ignore


def public_manifest_name(name: str) -> str:
    """Map a hidden source manifest onto a non-hidden stored name."""
    text = str(name or "").strip()
    if text == ".mcp.json":
        return "mcp.json"
    if is_hidden_name(text):
        return text.lstrip(".") or text
    return text


__all__ = [
    "JUNK_DIR_NAMES",
    "copytree_ignore",
    "is_hidden_name",
    "is_junk_dir_name",
    "public_manifest_name",
    "skip_dir_name",
    "skip_file_name",
    "skip_path",
    "skip_rel_path",
]
