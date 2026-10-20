"""Public entry surface for skill ``scripts/run.py``."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from binary import NativeBinaryResolver
from context import SkillContext
from runners import (
    BinaryRunner,
    CliRunner,
    ReportRunner,
    main,
)
from workspace import WorkspaceStore

__all__ = [
    "main",
    "run_binary",
    "run_cli",
    "run_report",
    "workspace_path",
]

_ctx = SkillContext()
_resolver = NativeBinaryResolver()
_store = WorkspaceStore(_ctx)


def workspace_path() -> Path:
    return _store.path()


def run_binary(
    binary: str,
    *,
    default_for_target: Callable[[str], str] | None = None,
    timeout: int = 300,
) -> int:
    return BinaryRunner(
        binary,
        default_for_target=default_for_target,
        timeout=timeout,
        context=_ctx,
        resolver=_resolver,
        store=_store,
    ).run()


def run_cli(
    handler: Callable[[list[str]], int],
    *,
    usage: str = "",
) -> int:
    return CliRunner(handler, usage=usage, context=_ctx).run()


def run_report(builder: Callable[[Path], str | Path]) -> int:
    return ReportRunner(builder, context=_ctx, store=_store).run()
