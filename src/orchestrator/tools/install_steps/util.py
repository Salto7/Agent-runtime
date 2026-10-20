"""Shared helpers for catalog install steps + install-doc fences."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any


from agent_runtime.api import Session

_REPO_RE = re.compile(
    r"^(?:https?://github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
_GH_SCRIPT = Path(__file__).resolve().parent.parent / "catalog" / "assets" / "install_github_release.sh"


def _run(
    cmd: list[str] | str, *, timeout: int = 300, shell: bool = False
) -> tuple[int, str, str]:
    res = Session.current().exec(cmd, timeout=timeout, shell=shell)
    return res.code, res.stdout, res.stderr


def _pkgs(raw: dict[str, Any]) -> list[str]:
    return [str(p).strip() for p in (raw.get("packages") or []) if str(p).strip()]


def _apt_package_installed(package: str) -> bool:
    """True when dpkg reports the package installed (Kali often preloads these)."""
    pkg = (package or "").strip()
    if not pkg:
        return False
    code, out, _err = _run(
        ["dpkg-query", "-W", "-f=${Status}", pkg],
        timeout=30,
    )
    return code == 0 and "install ok installed" in (out or "")


_APT_ENV_ALIASES: tuple[tuple[str, str], ...] = (
    ("python", "python3"),
    ("node", "nodejs"),
)


def _ensure_env_command_aliases(installed_packages: Iterable[str]) -> None:
    pkgs = {p.strip().lower() for p in installed_packages}
    for alias, target in _APT_ENV_ALIASES:
        if target not in pkgs:
            continue
        _run(
            [
                "bash",
                "-lc",
                f"command -v {alias} >/dev/null 2>&1 || "
                f"{{ command -v {target} >/dev/null 2>&1 && "
                f'ln -sfn "$(command -v {target})" "/usr/local/bin/{alias}"; }}',
            ],
            timeout=30,
        )


def _under_catalog(catalog_dir: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(catalog_dir.resolve())
        return True
    except (OSError, ValueError):
        return False
