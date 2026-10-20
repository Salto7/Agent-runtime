"""Sandbox shell: provision missing CLIs and run / periodic commands."""

from __future__ import annotations

import re
import shlex
import time

from agent_runtime.api import Session
from orchestrator.prompts import INSTALL_MISSING_HINT
from orchestrator.sandbox.tool_output import ShellResult
from orchestrator.tools.install import InstallResolver, cli_on_path
from orchestrator.utils.service import SharedServiceBase
from orchestrator.utils.stream import StreamEmitter

# Compound / redirected shell is executed as-is — never treated as an apt binary.
_COMPOUND_SHELL = re.compile(r"(?:&&|\|\||[;|`\n<>]|\$\(|\$\{)")
_BIN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9._+-]*$")
# POSIX / common shell builtins — skip install resolver.
_SHELL_BUILTINS = frozenset(
    {
        ".",
        ":",
        "[",
        "alias",
        "bg",
        "break",
        "builtin",
        "cd",
        "command",
        "continue",
        "declare",
        "echo",
        "eval",
        "exec",
        "exit",
        "export",
        "false",
        "fg",
        "getopts",
        "hash",
        "jobs",
        "kill",
        "local",
        "printf",
        "pwd",
        "read",
        "readonly",
        "return",
        "set",
        "shift",
        "source",
        "test",
        "times",
        "trap",
        "true",
        "type",
        "ulimit",
        "umask",
        "unalias",
        "unset",
        "wait",
    }
)


class ProvisionService(SharedServiceBase):
    """Resolve and install binaries for sandbox shell runs."""

    @staticmethod
    def command_binary(command: str) -> str:
        """Return a provisionable binary name, or ``\"\"`` to skip install.

        Only simple ``binary [args…]`` forms qualify. Compound shell, redirects,
        substitutions, and assignments are executed without a provision pass.
        """
        cmd = (command or "").strip()
        if not cmd or _COMPOUND_SHELL.search(cmd):
            return ""
        if re.match(r"^\w+=", cmd):
            return ""
        try:
            tokens = shlex.split(cmd, posix=True)
        except ValueError:
            return ""
        if not tokens:
            return ""
        token = tokens[0]
        if token.startswith("-") or "/" in token or "\\" in token:
            return ""
        if not _BIN_NAME.fullmatch(token):
            return ""
        return token

    def provision_command(self, command: str, *, package: str = "") -> tuple[bool, str]:
        binary = self.command_binary(command)
        if not binary:
            return True, ""
        if binary in _SHELL_BUILTINS:
            return True, ""
        # Cascade: Kali PATH → tools/catalog → LLM (see InstallResolver).
        if cli_on_path(binary):
            return True, f"{binary} already installed (Kali PATH)"

        ok, msg = InstallResolver.shared().resolve(binary, package=package)
        if not ok and INSTALL_MISSING_HINT not in msg:
            msg = f"{msg}. {INSTALL_MISSING_HINT}"
        return ok, msg


class ShellRunner(SharedServiceBase):
    """Run provisioned shell commands through the bound sandbox."""

    def __init__(
        self,
        *,
        provision: ProvisionService | None = None,
        stream: StreamEmitter | None = None,
    ) -> None:
        self._provision = provision or ProvisionService.shared()
        self._stream = stream or StreamEmitter.shared()

    def _announce(self, ok: bool, msg: str) -> int | None:
        if msg and "already" not in msg:
            self._stream.emit_log(msg)
        if not ok:
            if msg:
                self._stream.emit_stderr(msg)
            return 1
        return None

    DEFAULT_TIMEOUT_S = 1800.0   # no command may block a job forever

    def _exec_shell(self, cmd: str, *, timeout: float | None = None) -> ShellResult:
        """Run ``cmd``, mirror its output to the operator stream, return it."""
        res = Session.current().exec(cmd, timeout=timeout or self.DEFAULT_TIMEOUT_S, shell=True)
        code = res.code if res.code is not None else 1
        if res.stdout:
            self._stream.emit_stdout(res.stdout, exit_code=code)
        if res.stderr:
            self._stream.emit_stderr(res.stderr, exit_code=code)
        return ShellResult(code, res.stdout or "", res.stderr or "")

    def run_shell_result(self, command: str, *, package: str = "") -> ShellResult:
        """Provision + run; the result carries the output for the caller."""
        cmd = (command or "").strip()
        if not cmd:
            msg = "usage: '<shell command>' [apt_package]"
            self._stream.emit_stderr(msg)
            return ShellResult(2, "", msg)
        ok, msg = self._provision.provision_command(cmd, package=package)
        err = self._announce(ok, msg)
        if err is not None:
            return ShellResult(err, "", msg)
        return self._exec_shell(cmd)


    def run_shell(self, command: str, *, package: str = "") -> int:
        return self.run_shell_result(command, package=package).code

    def run_periodic(
        self,
        command: str,
        *,
        interval_seconds: int,
        duration_seconds: int,
        package: str = "",
    ) -> int:
        return self.run_periodic_result(
            command,
            interval_seconds=interval_seconds,
            duration_seconds=duration_seconds,
            package=package,
        ).code

    def run_periodic_result(
        self,
        command: str,
        *,
        interval_seconds: int,
        duration_seconds: int,
        package: str = "",
    ) -> ShellResult:
        """Like :meth:`run_periodic`; returns the last run's output."""
        cmd = (command or "").strip()
        if not cmd:
            msg = "usage: command + interval_seconds + duration_seconds"
            self._stream.emit_stderr(msg)
            return ShellResult(2, "", msg)
        interval = max(1, int(interval_seconds))
        total = max(interval, int(duration_seconds))
        ok, msg = self._provision.provision_command(cmd, package=package)
        if msg and "already" not in msg:
            self._stream.emit_log(msg)
        if not ok:
            if msg:
                self._stream.emit_stderr(msg)
            return ShellResult(1, "", msg)
        deadline = time.time() + total
        tick = min(max(interval * 2, 60), 600)
        iteration = 0
        last = ShellResult(0)
        self._stream.emit_log(f"Periodic: {cmd!r} every {interval}s for {total}s")
        while time.time() < deadline:
            iteration += 1
            self._stream.emit_log(f"--- run {iteration}: {cmd} ---")
            last = self._exec_shell(cmd, timeout=tick)
            self._stream.stream(
                "log",
                f"exit_code={last.code}",
                iteration=iteration,
                timed_out=last.code == 124,
            )
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))
        self._stream.emit_log(f"--- finished {iteration} run(s) in {total}s ---")
        return last
