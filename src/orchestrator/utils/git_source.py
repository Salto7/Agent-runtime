"""Normalize and shallow-clone a git source (``owner/repo`` or a full URL).

Shared by the MCP installer and the marketplace fetcher — one place that
knows how to turn a user-typed string into a real clone, with the same
input validation either caller relies on.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_GIT_URL = re.compile(r"^(?:git@|https?://|ssh://|git://).+", re.I)
_OWNER_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def normalize_git_source(source: str) -> str:
    text = str(source or "").strip()
    if not text or any(ch in text for ch in "\n\r\x00"):
        raise ValueError("git source is empty or invalid")
    if _OWNER_REPO.match(text):
        return f"https://github.com/{text}.git"
    if _GIT_URL.match(text) or text.endswith(".git"):
        return text
    raise ValueError("git source must be owner/repo or a git URL")


def git_clone(url: str, dest: Path, *, revision: str = "", depth: int = 1) -> None:
    cmd = ["git", "clone", "--depth", str(max(1, depth))]
    rev = str(revision or "").strip()
    if rev:
        cmd.extend(["--branch", rev])
    cmd.extend([url, str(dest)])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("git is required to install from a repository") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(detail or "git clone failed")
    if not dest.is_dir():
        raise RuntimeError("git clone produced no directory")
