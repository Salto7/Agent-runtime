"""Resolve native CLIs, skipping Python console-script wrappers."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_WRAPPER_MARKERS = (
    "load_entry_point",
    "pkg_resources",
    "importlib.metadata",
    "from pkgutil",
    "console_scripts",
)


class NativeBinaryResolver:
    """Prefer ELF under /usr/local/bin; refuse pip/setuptools wrappers."""

    def resolve(self, binary: str) -> str | None:
        name = (binary or "").strip()
        if not name or "/" in name:
            return None
        ordered: list[str] = []
        for candidate in (
            f"/usr/local/bin/{name}",
            f"/usr/bin/{name}",
            shutil.which(name) or "",
        ):
            if candidate and candidate not in ordered:
                ordered.append(candidate)

        for path in ordered:
            p = Path(path)
            if p.is_file() and os.access(p, os.X_OK) and self.is_elf(str(p)):
                return str(p)
        for path in ordered:
            p = Path(path)
            if p.is_file() and os.access(p, os.X_OK) and not self.is_python_wrapper(str(p)):
                return str(p)
        return None

    @staticmethod
    def file_head(path: str, n: int = 240) -> bytes:
        try:
            return Path(path).read_bytes()[:n]
        except OSError:
            return b""

    @classmethod
    def is_elf(cls, path: str) -> bool:
        return cls.file_head(path, 4).startswith(b"\x7fELF")

    @classmethod
    def is_python_wrapper(cls, path: str) -> bool:
        if cls.is_elf(path):
            return False
        head = cls.file_head(path)
        if not head:
            return False
        text = head.decode("utf-8", errors="ignore")
        first = text.split("\n", 1)[0].lower()
        if not (first.startswith("#!") and "python" in first):
            return False
        if any(m in text for m in _WRAPPER_MARKERS):
            return True
        try:
            return Path(path).stat().st_size < 8192
        except OSError:
            return True
