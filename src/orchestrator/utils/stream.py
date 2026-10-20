"""Job stream emit over ORCHESTRATOR_STREAM_SOCKET (AF_UNIX)."""

from __future__ import annotations

import json
import os
import socket
import sys

from orchestrator.utils.job_env import JobEnv
from orchestrator.utils.service import SharedServiceBase


class StreamEmitter(SharedServiceBase):
    """Push job messages to ORCHESTRATOR_STREAM_SOCKET when present."""

    def stream(self, message_type: str, content: str, **metadata) -> None:
        job_id = JobEnv.get("ORCHESTRATOR_JOB_ID")
        path = JobEnv.get("ORCHESTRATOR_STREAM_SOCKET")
        if not job_id or not path or not content or not os.path.exists(path):
            return
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(5)
                sock.connect(path)
                sock.sendall(
                    (
                        json.dumps(
                            {
                                "job_id": job_id,
                                "message_type": message_type,
                                "content": content,
                                "metadata": metadata or {},
                            }
                        )
                        + "\n"
                    ).encode()
                )
        except OSError:
            pass

    def emit_stdout(self, text: str, **metadata) -> None:
        self._emit("stdout", text, **metadata)

    def emit_stderr(self, text: str, **metadata) -> None:
        """``exit_code=0`` marks tool chatter (banners, traces), not a failure."""
        self._emit("stderr", text, file=sys.stderr, **metadata)

    def emit_log(self, text: str) -> None:
        if not text:
            return
        self.stream("log", text)
        print(text, flush=True)

    def _emit(self, kind: str, text: str, *, file=None, **metadata) -> None:
        if not text:
            return
        self.stream(kind, text.rstrip("\n"), **metadata)
        print(
            text,
            end="" if text.endswith("\n") else "\n",
            file=file or sys.stdout,
            flush=True,
        )
