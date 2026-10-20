"""Install skill packs from disk, git, or openskills/npx marketplace sources."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from orchestrator.skills.install.dragdrop import install_directory
from orchestrator.skills.install.openskills import install_openskills

_GIT_URL = re.compile(
    r"^(?:git@|https?://|ssh://|git://).+",
    re.I,
)
_OWNER_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def install_skill_source(kind: str, value: str, *, revision: str = "") -> list[str]:
    """Install one or more skill packs. ``kind`` is disk | git | npx."""
    mode = str(kind or "").strip().lower()
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("source value is required")
    if mode == "disk":
        return install_directory(_safe_dir(raw), origin=f"disk:{raw}")
    if mode == "git":
        return _install_git(raw, revision=revision)
    if mode in {"npx", "openskills", "marketplace"}:
        return install_openskills(raw, revision=revision)
    raise ValueError("kind must be disk, git, or npx")


def _safe_dir(raw: str) -> Path:
    text = str(raw or "").strip()
    if not text or text.startswith("-") or any(ch in text for ch in "\n\r\x00"):
        raise ValueError("path is empty or invalid")
    path = Path(text).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"not a directory: {path}")
    return path


def _install_git(source: str, *, revision: str = "") -> list[str]:
    origin = _normalize_git(source)
    with tempfile.TemporaryDirectory(prefix="orchestrator-skill-git-") as tmp:
        dest = Path(tmp) / "repo"
        _git_clone(origin, dest, revision=revision)
        return install_directory(dest, origin=f"git:{origin}")


def _normalize_git(source: str) -> str:
    text = str(source or "").strip()
    if not text or any(ch in text for ch in "\n\r\x00"):
        raise ValueError("git source is empty or invalid")
    if _OWNER_REPO.match(text):
        return f"https://github.com/{text}.git"
    if _GIT_URL.match(text) or text.endswith(".git"):
        return text
    raise ValueError("git source must be owner/repo or a git URL")


def _git_clone(url: str, dest: Path, *, revision: str = "") -> None:
    cmd = ["git", "clone", "--depth", "1"]
    rev = str(revision or "").strip()
    if rev:
        cmd.extend(["--branch", rev])
    cmd.extend(["--", url, str(dest)])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git is required to install from a repository") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(detail or "git clone failed")
    if not dest.is_dir():
        raise RuntimeError("git clone produced no directory")


