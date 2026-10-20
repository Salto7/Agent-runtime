"""PTY spawn used only by runtime backends.

One pump thread owns the master fd and appends output to a bounded scrollback
buffer. Readers pull by cursor (``read_since``), so several viewers (a refreshed
tab, a stale stream that has not noticed its client left) never steal bytes from
each other, and a reconnect replays recent output instead of a blank screen.
"""

from __future__ import annotations

import os
import select
import threading
import time

from ptyprocess import PtyProcess

from agent_runtime.api import TerminalHandle

_READ_CHUNK = 16_384
_MAX_INPUT = 64 * 1024
_SCROLLBACK = 256 * 1024


def _clamp(value: int, default: int) -> int:
    try:
        return max(1, min(int(value or default), 500))
    except (TypeError, ValueError):
        return default


class PtyTerminal(TerminalHandle):
    def __init__(self, name: str, *, argv: list[str], cols: int, rows: int) -> None:
        super().__init__(name)
        self.cols = _clamp(cols, 80)
        self.rows = _clamp(rows, 24)
        self.last_active = time.time()
        self._write_lock = threading.Lock()
        self._cond = threading.Condition()
        self._buf = bytearray()
        self._base = 0  # absolute offset of ``_buf[0]``
        self._eof = False
        self._closed = False
        # New session + controlling tty, so resizes deliver SIGWINCH to ``docker exec -t``.
        self._proc = PtyProcess.spawn(argv, dimensions=(self.rows, self.cols))
        self._master = self._proc.fd
        self._pump = threading.Thread(
            target=self._pump_loop, name=f"pty-{name}", daemon=True
        )
        self._pump.start()

    # -- output -----------------------------------------------------------

    def _pump_loop(self) -> None:
        fd = self._master
        while not self._closed:
            try:
                ready, _, _ = select.select([fd], [], [], 0.5)
            except (ValueError, OSError):
                break
            if not ready:
                if not self._proc.isalive():
                    break
                continue
            try:
                chunk = os.read(fd, _READ_CHUNK)
            except OSError:
                # EIO once the child side closes.
                break
            if not chunk:
                break
            with self._cond:
                self._buf += chunk
                overflow = len(self._buf) - _SCROLLBACK
                if overflow > 0:
                    del self._buf[:overflow]
                    self._base += overflow
                self.last_active = time.time()
                self._cond.notify_all()
        with self._cond:
            self._eof = True
            self._cond.notify_all()

    def cursor(self) -> int:
        """Absolute offset of the oldest byte still in scrollback."""
        with self._cond:
            return self._base

    def read_since(self, cursor: int, timeout: float = 0.25) -> tuple[bytes, int, bool]:
        """Return ``(bytes, next_cursor, eof)``; waits up to ``timeout`` for output."""
        with self._cond:
            end = self._base + len(self._buf)
            if cursor >= end and not self._eof:
                self._cond.wait(timeout)
                end = self._base + len(self._buf)
            start = max(cursor, self._base)
            data = bytes(self._buf[start - self._base:])
            return data, end, self._eof and not data

    def read(self, timeout: float = 0.25) -> bytes:
        # Kept for the ``TerminalHandle`` contract; returns only new bytes.
        cursor = getattr(self, "_legacy_cursor", None)
        if cursor is None:
            with self._cond:
                cursor = self._base + len(self._buf)
        data, self._legacy_cursor, _ = self.read_since(cursor, timeout)
        return data

    # -- input / control --------------------------------------------------

    def touch(self) -> None:
        self.last_active = time.time()

    def write(self, data: bytes) -> None:
        if not data:
            return
        data = data[:_MAX_INPUT]
        with self._write_lock:
            self.touch()
            view = memoryview(data)
            while view:
                written = os.write(self._master, view)
                view = view[written:]

    def resize(self, cols: int, rows: int) -> None:
        cols, rows = _clamp(cols, self.cols), _clamp(rows, self.rows)
        with self._write_lock:
            self.touch()
            if (cols, rows) == (self.cols, self.rows):
                return
            self.cols, self.rows = cols, rows
            try:
                self._proc.setwinsize(rows, cols)
            except OSError:
                pass

    def alive(self) -> bool:
        return self._proc.isalive() and not self._eof

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._proc.terminate(force=True)
        except Exception:
            pass
        self._pump.join(timeout=1.5)
        try:
            self._proc.close(force=True)
        except Exception:
            pass
        with self._cond:
            self._eof = True
            self._cond.notify_all()
