"""Skill-side stream (Unix socket).

Lives under ``skills/helpers/``. No peon/orchestrator imports — skills stay
runnable outside the control plane. Stream is a no-op when
ORCHESTRATOR_STREAM_SOCKET / job id are unset.
"""

from __future__ import annotations

import builtins
import json
import os
import socket
import threading
from typing import Any

_lock = threading.Lock()
_sock: socket.socket | None = None


def _job_id() -> str:
    return (
        os.environ.get("ORCHESTRATOR_JOB_ID", "").strip()
        or os.environ.get("ORCHESTRATOR_TASK_ID", "").strip()
    )


def stream(message_type: str, content: str, **metadata: Any) -> None:
    """NDJSON line to the orchestrator stream socket (best-effort)."""
    global _sock
    job_id = _job_id()
    path = os.environ.get("ORCHESTRATOR_STREAM_SOCKET", "").strip()
    if not job_id or not path or not content or not os.path.exists(path):
        return
    meta = dict(metadata or {})
    meta.setdefault("job_id", job_id)
    project = os.environ.get("ORCHESTRATOR_PROJECT_ID", "").strip()
    if project:
        meta.setdefault("project_id", project)
    line = (
        json.dumps(
            {
                "job_id": job_id,
                "task_id": job_id,
                "message_type": message_type,
                "content": content,
                "metadata": meta,
            }
        )
        + "\n"
    )
    try:
        with _lock:
            if _sock is None:
                _sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                _sock.settimeout(5)
                _sock.connect(path)
            _sock.sendall(line.encode())
    except OSError:
        _sock = None


def _streaming_print(*args: Any, **kwargs: Any) -> None:
    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    text = sep.join(str(a) for a in args) + end
    builtins.print(*args, **kwargs)
    if text.strip():
        stream("stdout", text.rstrip("\n"))


# Skills that `from orchestrator_tools import print` get the streaming print.
print = _streaming_print  # noqa: A001
