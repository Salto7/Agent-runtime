"""Unix-socket stream server (host listens; skills/clients connect and push NDJSON).

Ported from Peon's ``StreamSocketServer`` — Django-free. Pair with
``skills/helpers/orchestrator_tools.stream`` / ``StreamEmitter``.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_RECV_SIZE = 65_536
_CLIENT_TIMEOUT = 30.0
_ACCEPT_TIMEOUT = 1.0
_LISTEN_BACKLOG = 128
_HANDLER_WORKERS = 8
_MAX_BUFFER = 4 * 1024 * 1024


class StreamSocketServer:
    """Accept AF_UNIX clients; each line is one JSON object passed to on_message."""

    def __init__(self, socket_path: str, on_message: Callable[[dict], None]):
        self.socket_path = socket_path
        self.on_message = on_message
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        parent = os.path.dirname(self.socket_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        self._pool = ThreadPoolExecutor(
            max_workers=_HANDLER_WORKERS, thread_name_prefix="stream-sock"
        )
        self._running = True
        self._thread = threading.Thread(
            target=self._serve, name="stream-sock-accept", daemon=True
        )
        self._thread.start()
        for _ in range(200):  # return only once the socket is bound: clients check for the file and drop if absent
            if os.path.exists(self.socket_path):
                break
            time.sleep(0.01)
        logger.info("Stream Unix socket listening on %s", self.socket_path)

    def stop(self) -> None:
        self._running = False
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

    def _serve(self) -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server = server
        try:
            server.bind(self.socket_path)
            os.chmod(self.socket_path, 0o666)
            server.listen(_LISTEN_BACKLOG)
            server.settimeout(_ACCEPT_TIMEOUT)
            while self._running:
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._running:
                        logger.exception("Unix socket accept error")
                    break
                pool = self._pool
                if pool is None:
                    conn.close()
                    continue
                pool.submit(self._handle_client, conn)
        finally:
            try:
                server.close()
            except OSError:
                pass

    def _handle_client(self, conn: socket.socket) -> None:
        buf = b""
        try:
            conn.settimeout(_CLIENT_TIMEOUT)
            while self._running:
                chunk = conn.recv(_RECV_SIZE)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > _MAX_BUFFER:
                    logger.warning("Stream socket client exceeded buffer; closing")
                    break
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("Invalid JSON on stream socket: %s", line[:100])
                        continue
                    if not isinstance(payload, dict):
                        continue
                    try:
                        self.on_message(payload)
                    except Exception:
                        logger.exception("Stream socket on_message failed")
        except (ConnectionResetError, TimeoutError, OSError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
