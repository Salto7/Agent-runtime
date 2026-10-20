"""Safe zip extraction, shared by the skill drag-drop installer and the
marketplace scanner. Rejects path traversal (``..``, absolute paths);
callers decide where the extracted tree lives and how long it persists.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from orchestrator.utils.paths import normalize_rel_path, safe_join


def extract_zip_safe(data: bytes, dest_dir: Path) -> Path:
    """Extract ``data`` into ``dest_dir``. Raises ``ValueError`` on an unsafe path."""
    if not data:
        raise ValueError("empty zip")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                rel = normalize_rel_path(info.filename.rstrip("/"))
                if rel is None and info.filename.strip("/"):
                    raise ValueError(f"unsafe zip path {info.filename!r}")
                continue
            rel = normalize_rel_path(info.filename)
            target = safe_join(dest, rel)
            if target is None:
                raise ValueError(f"unsafe zip path {info.filename!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))
    return dest
