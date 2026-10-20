"""Interpreter selection from a script shebang or suffix. Not from skill prose."""

from __future__ import annotations

import shlex
from pathlib import Path

_SUFFIX: dict[str, tuple[str, ...]] = {
    ".py": ("python3",),
    ".sh": ("bash",),
    ".bash": ("bash",),
    ".js": ("node",),
    ".mjs": ("node",),
    ".cjs": ("node",),
}
_RUNTIME_NAMES = frozenset(
    {"python", "python3", "bash", "sh", "node"}
    | {name for names in _SUFFIX.values() for name in names}
)

# ``env`` options that take a following argument.
_ENV_VALUE_FLAGS = frozenset({"-u", "-C", "--unset", "--chdir"})


def _shebang_argv(path: Path) -> list[str] | None:
    try:
        with path.open("rb") as handle:
            first = handle.readline(512)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    text = first[2:].decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        parts = shlex.split(text)
    except ValueError:
        return None
    if not parts:
        return None
    if Path(parts[0]).name != "env":
        return _prefer_path_binaries(parts)
    # ``#!/usr/bin/env [-S] interpreter [args…]``
    rest = parts[1:]
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "-S":
            i += 1
            continue
        if tok in _ENV_VALUE_FLAGS:
            i += 2
            continue
        if tok.startswith("--") and "=" in tok:
            i += 1
            continue
        if tok.startswith("-") and not tok.startswith("--"):
            i += 1
            continue
        break
    if i >= len(rest):
        return None
    return _prefer_path_binaries(rest[i:])


def _prefer_path_binaries(parts: list[str]) -> list[str]:
    """Use a bare runtime name when the shebang path is a known sandbox binary."""
    if not parts:
        return parts
    name = Path(parts[0]).name
    if name in _RUNTIME_NAMES:
        return [name, *parts[1:]]
    return parts


def interpreter_for_source(name: str, text: str) -> list[str] | None:
    """Same rules as ``interpreter_argv`` for catalog text that is not on disk."""
    first = (text or "").splitlines()[:1]
    line = first[0] if first else ""
    if line.startswith("#!"):
        body = line[2:].strip()
        try:
            parts = shlex.split(body)
        except ValueError:
            parts = []
        if parts:
            runtime = Path(parts[0]).name
            if runtime in _RUNTIME_NAMES:
                return [runtime, *parts[1:]]
            return parts
    suffix = Path(name).suffix.lower()
    if suffix in _SUFFIX:
        return list(_SUFFIX[suffix])
    return None


def interpreter_argv(path: Path) -> list[str] | None:
    """Argv prefix for ``path``, ``[]`` when the file itself is executable.

    Shebang wins over file suffix (Claude packs often use ``.sh`` with a
    non-bash interpreter). ``None`` means no interpreter was declared.
    """
    shebang = _shebang_argv(path)
    if shebang is not None:
        return shebang
    suffix = path.suffix.lower()
    if suffix in _SUFFIX:
        return list(_SUFFIX[suffix])
    try:
        executable = bool(path.stat().st_mode & 0o111)
    except OSError:
        executable = False
    if executable:
        return []
    return None
