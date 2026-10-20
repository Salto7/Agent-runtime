"""What a command tool returns to the model: exit code + output, never silent.

The operator stream already shows full output; the *model* only sees the tool's
return value. Returning just an exit code makes an agent blind to its own
commands. Output that is too large for the model's context is not discarded:
the full text is written to ``workspace/tool-output/`` in the sandbox and the
result carries a head, a tail and that path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

# Characters of one stream (stdout / stderr) handed to the model inline.
INLINE_STDOUT = 12_000
INLINE_STDERR = 4_000
_SPILL_DIR = "/workspace/workspace/tool-output"
_SPILL_REL = "workspace/tool-output"


@dataclass(frozen=True)
class ShellResult:
    code: int
    stdout: str = ""
    stderr: str = ""


def _spill(session, label: str, kind: str, text: str) -> str:
    """Persist ``text`` in the sandbox workspace; return its relative path or ''."""
    try:
        from orchestrator.sandbox.materialize import materialize_tree

        name = f"{label}-{time.time_ns()}-{kind}.txt"
        if materialize_tree(session, _SPILL_DIR, {name: text}):
            return ""
        return f"{_SPILL_REL}/{name}"
    except Exception:  # noqa: BLE001 — never break a tool result over a spill
        return ""


def _inline(session, label: str, kind: str, text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head_n = int(limit * 0.6)
    tail_n = limit - head_n
    omitted = len(text) - limit
    path = _spill(session, label, kind, text)
    where = (
        f"full {kind} saved to {path} (read_workspace_artifact, or sed/grep it)"
        if path
        else f"{kind} was longer than the inline limit"
    )
    return (
        f"{text[:head_n].rstrip()}\n…[{omitted} chars omitted — {where}]…\n"
        f"{text[-tail_n:].lstrip()}"
    )


def render_tool_output(
    session, result: ShellResult, *, label: str = "cmd"
) -> str:
    """``exit=N`` plus stdout/stderr, spilled to a file when oversized."""
    lines = [f"exit={result.code}"]
    out = _inline(session, label, "stdout", result.stdout, INLINE_STDOUT)
    err = _inline(session, label, "stderr", result.stderr, INLINE_STDERR)
    if out:
        lines.append(out)
    if err:
        lines.append(f"[stderr]\n{err}" if out else err)
    if not out and not err:
        lines.append("(no output)")
    return "\n".join(lines)
