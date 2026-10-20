"""Write catalog file bodies into a bound sandbox. No host catalog mount."""

from __future__ import annotations

import json
from typing import Mapping

_WRITER = r"""
import json, sys
from pathlib import Path
spec = json.load(sys.stdin)
root = Path(spec["dest"])
if not root.is_absolute():
    raise SystemExit("dest must be absolute")
root.mkdir(parents=True, exist_ok=True)
for rel, text in (spec.get("files") or {}).items():
    path = Path(rel)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit("unsafe path")
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text if isinstance(text, str) else str(text), encoding="utf-8")
print("ok")
"""


def materialize_tree(session, dest: str, files: Mapping[str, str]) -> str:
    """Return ``\"\"`` on success, otherwise an error string."""
    target = str(dest or "").strip()
    if not target.startswith("/"):
        return "Error: materialize dest must be an absolute container path."
    payload = json.dumps({"dest": target, "files": dict(files or {})})
    result = session.exec(
        ["python3", "-c", _WRITER],
        stdin=payload,
        timeout=120,
    )
    if result.ok and "ok" in (result.stdout or ""):
        return ""
    detail = (result.stderr or result.stdout or "materialize failed").strip()
    return f"Error: could not write {target}: {detail[:500]}"
