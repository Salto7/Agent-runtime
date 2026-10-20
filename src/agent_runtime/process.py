"""Subprocess helper for runtime backends only.

Role assets, catalog CLIs, and operator commands go through ``RuntimeSession.exec``.
"""

from __future__ import annotations

import os
import shlex
import subprocess

from agent_runtime.api import ExecResult


class HostProcessRunner:
    """Typed host subprocess boundary shared by runtime backends."""

    @staticmethod
    def run(
        cmd: list[str] | str,
        *,
        timeout: float | None = 300,
        shell: bool = False,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
    ) -> ExecResult:
        merged = {**os.environ, "DEBIAN_FRONTEND": "noninteractive", **(env or {})}
        try:
            proc = subprocess.run(
                cmd,
                shell=shell,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=merged,
                input=stdin,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            def _text(raw: object) -> str:
                return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else (raw or "")

            out = _text(exc.stdout)
            err = _text(exc.stderr) or f"timed out after {timeout}s"
            return ExecResult(124, out, err)
        return ExecResult(proc.returncode or 0, proc.stdout or "", proc.stderr or "")


class ExecTransport:
    """Backend-neutral remote command construction."""

    @staticmethod
    def command(cmd: list[str] | str, *, shell: bool = False) -> list[str]:
        if shell or isinstance(cmd, str):
            body = cmd if isinstance(cmd, str) else " ".join(shlex.quote(x) for x in cmd)
            return ["bash", "-lc", body]
        return list(cmd)

    @classmethod
    def command_in(
        cls,
        cmd: list[str] | str,
        *,
        cwd: str,
        shell: bool = False,
    ) -> list[str]:
        inner = cls.command(cmd, shell=shell)
        if inner[:2] == ["bash", "-lc"]:
            body = inner[2]
        else:
            body = " ".join(shlex.quote(x) for x in inner)
        return ["bash", "-lc", f"cd {shlex.quote(cwd)} && {body}"]
