#!/usr/bin/env python3
"""Thin orchestrator client — list / suggest / plan / execute demos.

One-shot::

    python examples/thin_client.py list-skills
    python examples/thin_client.py list-tools
    python examples/thin_client.py suggest-tools "add a yq fuzzer for YAML diffing"
    # (TTY) prompts to test installer in learn-lab; on fail, asks for a fix prompt
    python examples/thin_client.py suggest-skills "filter a JSON API response"
    python examples/thin_client.py draft-skill "fetch a URL and summarize its headers"
    python examples/thin_client.py lint-skill json-filter
    python examples/thin_client.py list-roles
    python examples/thin_client.py plan "fetch https://example.com and extract the title"
    python examples/thin_client.py run "write hello.txt in /workspace" --role builder --remove
    python examples/thin_client.py run "research X and report" --project --role researcher
    python examples/thin_client.py dream
    python examples/thin_client.py test-tool tools/catalog/jq.yaml

Interactive console (no subcommand)::

    python examples/thin_client.py
    orch> help
    orch> list-tools
    orch> plan "fetch https://example.com and extract the title"
    # Tab / double-Tab completes and lists commands; Ctrl+C exits (stops learn-lab if running)
"""

from __future__ import annotations

import argparse
import json
import logging
import readline
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

from orchestrator import Orchestrator
from orchestrator.learn import LearnAuthoring
from orchestrator.learn.lab import LearnLab

META_COMMANDS = ("help", "commands", "?", "exit", "quit")
PROMPT = "orch> "

logger = logging.getLogger("thin_client")


def _configure_logging() -> None:
    """Route INFO+ to stdout and WARNING+ to stderr with bare messages."""
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    logger.propagate = False

    out = logging.StreamHandler(sys.stdout)
    out.setLevel(logging.INFO)
    out.addFilter(lambda record: record.levelno < logging.WARNING)
    out.setFormatter(logging.Formatter("%(message)s"))

    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    err.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(out)
    logger.addHandler(err)


def _emit(data: object) -> None:
    if isinstance(data, (dict, list)):
        logger.info("%s", json.dumps(data, indent=2, default=str))
    else:
        logger.info("%s", data)


def _stop_learn_lab() -> None:
    """Remove learn-lab container(s) if present (safe no-op when absent)."""
    try:
        result = LearnLab.shared().delete()
    except Exception as exc:
        logger.warning("learn-lab cleanup failed: %s", exc)
        return
    if result.get("removed"):
        logger.info("Stopped learn-lab (%s).", result.get("name") or "learn-lab")
    elif not result.get("ok"):
        logger.warning(
            "learn-lab cleanup: %s",
            result.get("error") or result.get("reason") or result,
        )


def _on_keyboard_interrupt(message: str) -> None:
    _stop_learn_lab()
    logger.info("\n%s", message)


def _ask(prompt: str) -> str | None:
    """Read a line; return None on Ctrl+C / EOF."""
    try:
        return input(prompt)
    except KeyboardInterrupt:
        logger.info("")
        _on_keyboard_interrupt("Interrupted.")
        return None
    except EOFError:
        logger.info("")
        return None


def _ask_yes_no(question: str, *, default: bool = False) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    ans = _ask(question + suffix)
    if ans is None:
        return False
    text = ans.strip().lower()
    if not text:
        return default
    return text in ("y", "yes")


def _emit_suggest(result: dict) -> None:
    _emit({k: result[k] for k in ("id", "notes", "yaml", "install_script") if k in result})


def _emit_tool_test(result: dict) -> None:
    lab = result.get("lab") or {}
    if isinstance(lab, dict) and lab.get("name"):
        logger.info(
            "lab=%s image=%s running=%s",
            lab.get("name"),
            lab.get("image"),
            lab.get("running"),
        )
    logger.info("--- install ---")
    logger.info("%s", result.get("message") or "(no install message)")
    logger.info("--- verify ---")
    steps = result.get("verify_steps") or []
    if steps:
        for step in steps:
            cmd = step.get("command") or "?"
            code = step.get("code")
            ok = step.get("ok")
            logger.info("$ %s  (code=%s ok=%s)", cmd, code, ok)
            out = (step.get("stdout") or "").rstrip()
            err = (step.get("stderr") or "").rstrip()
            if out:
                logger.info("%s", out)
            if err:
                logger.error("%s", err)
    else:
        logger.info(
            "%s",
            result.get("verify_output") or result.get("log") or "(no verify output)",
        )
    if result.get("ok"):
        logger.info("status: OK")
    else:
        detail = result.get("message") or "install/verify failed"
        logger.error("status: FAIL — %s", detail)


def _suggest_test_loop(result: dict, *, prompt: str) -> int:
    """After suggest: optionally test in learn-lab; on failure, replan with prior context."""
    if not sys.stdin.isatty():
        return 0

    original = (prompt or "").strip()

    while True:
        if not _ask_yes_no("Test suggested installer in learn-lab?"):
            return 0

        logger.info("Starting fresh learn-lab sandbox…")
        try:
            test = LearnLab.shared().test_tool_install(
                yaml_text=str(result.get("yaml") or ""),
                install_script=str(result.get("install_script") or ""),
            )
        except KeyboardInterrupt:
            _on_keyboard_interrupt("Interrupted — install test cancelled.")
            raise
        except Exception as exc:
            test = {
                "ok": False,
                "message": str(exc),
                "verify_output": "",
                "verify_steps": [],
            }

        _emit_tool_test(test)
        if test.get("ok"):
            logger.info("Installer verified OK.")
            return 0

        if not _ask_yes_no("Re-suggest? (keeps original prompt + failure; optional fix)"):
            return 1

        fix = _ask("Fix guidance (optional, Enter to replan from error alone): ")
        if fix is None:
            return 1
        fix = fix.strip()

        err_bits = [
            str(test.get("message") or "").strip(),
            str(test.get("verify_output") or "").strip(),
        ]
        error = "\n".join(b for b in err_bits if b) or "(unknown)"

        logger.info("Replanning with original request + failed recipe…")
        try:
            result = LearnAuthoring.replan_tool(
                prompt=original or str(result.get("id") or "revise tool"),
                yaml_text=str(result.get("yaml") or ""),
                install_script=str(result.get("install_script") or ""),
                error=error,
                feedback=fix,
            )
        except Exception as exc:
            logger.error("error: re-suggest failed: %s", exc)
            return 1
        logger.info("")
        _emit_suggest(result)


def cmd_list_skills(orch: Orchestrator, args: argparse.Namespace) -> int:
    del args
    _emit(orch.list_skills())
    return 0


def cmd_list_roles(orch: Orchestrator, args: argparse.Namespace) -> int:
    del args
    _emit(orch.list_roles())
    return 0


def cmd_list_tools(orch: Orchestrator, args: argparse.Namespace) -> int:
    del args
    _emit(orch.list_tools())
    return 0


def cmd_suggest_tools(orch: Orchestrator, args: argparse.Namespace) -> int:
    del orch
    result = LearnAuthoring.suggest_tool(args.prompt)
    _emit_suggest(result)
    return _suggest_test_loop(result, prompt=args.prompt)


def cmd_suggest_skills(orch: Orchestrator, args: argparse.Namespace) -> int:
    _emit(orch.suggest_skills(args.prompt))
    return 0


def cmd_draft_skill(orch: Orchestrator, args: argparse.Namespace) -> int:
    del orch
    result = LearnAuthoring.write_skill(args.prompt)
    files = dict(result.get("files") or {})
    tool = result.get("tool_suggestion") or {}
    payload: dict = {
        "name": result.get("name"),
        "notes": result.get("notes"),
        "mode": result.get("mode"),
        "suggested_tools": result.get("suggested_tools"),
        "lint": result.get("lint"),
        "skill_md": (result.get("skill_md") or "")[:2000],
        "files": sorted(files),
        "scripts/run.py": (files.get("scripts/run.py") or "")[:2000] or None,
        "references/INSTALL.md": (files.get("references/INSTALL.md") or "")[:2000]
        or None,
    }
    if tool:
        payload["tool_suggestion"] = {
            "id": tool.get("id"),
            "notes": tool.get("notes"),
            "install_script": (tool.get("install_script") or "")[:500] or None,
            "yaml": (tool.get("yaml") or "")[:1500],
        }
    _emit(payload)
    return 0 if (result.get("lint") or {}).get("compatible") else 1


def cmd_lint_skill(orch: Orchestrator, args: argparse.Namespace) -> int:
    del orch
    try:
        path = Path(args.skill)
        if not path.is_dir():
            path = ROOT / "skills" / args.skill
        if not (path / "SKILL.md").is_file():
            raise ValueError(f"no SKILL.md in {path}")
        files = {
            p.relative_to(path).as_posix(): p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(path.rglob("*"))
            if p.is_file() and p.name != "SKILL.md" and "__pycache__" not in p.parts
        }
        result = LearnAuthoring.lint_skill(
            name=path.name, skill_md=(path / "SKILL.md").read_text(encoding="utf-8"), files=files
        )
    except ValueError as exc:
        logger.error("error: %s", exc)
        return 1
    _emit(result)
    return 0 if result.get("compatible") else 1


def cmd_plan(orch: Orchestrator, args: argparse.Namespace) -> int:
    result = orch.plan(args.prompt, mode=args.mode, role_ids=args.role or None)
    if result.error:
        logger.error("error: %s", result.error)
        return 1
    logger.info("# roles: %s\n", ", ".join(result.role_ids) or "(none)")
    logger.info("%s", result.plan)
    return 0 if result.ok else 1


def cmd_run(orch: Orchestrator, args: argparse.Namespace) -> int:
    """One role (default) or the whole crew (--project)."""
    if args.project:
        result = orch.run_project(
            args.prompt, role_ids=args.role or None, project_id=args.session,
            remove_sandbox=args.remove,
        )
    else:
        result = orch.run(
            args.prompt, role=(args.role[0] if args.role else ""), skill_names=args.skill or None,
            project_id=args.session, remove_sandbox=args.remove,
        )
    logger.info("%s", result.output or "(empty)")
    if result.error:
        logger.error("error: %s", result.error)
    logger.info("\n--- ok=%s status=%s roles=%s project=%s ---",
                result.ok, result.status, result.role_ids, result.project_id)
    return 0 if result.ok else 1


def cmd_memory_health(orch: Orchestrator, args: argparse.Namespace) -> int:
    del args
    health = orch.memory_health()
    _emit(health)
    return 0 if health.get("ok") else 1


def cmd_project(orch: Orchestrator, args: argparse.Namespace) -> int:
    """Plan + scheduled execution (objectives with dependencies, replan on failure)."""
    pid = orch.start_project(args.prompt, project_id=args.session)
    logger.info("project %s started", pid)
    state = orch.wait_project(pid, timeout=args.timeout)
    _emit(state)
    return 0 if state["status"] == "completed" else 1


def cmd_dream(orch: Orchestrator, args: argparse.Namespace) -> int:
    del args
    _emit(orch.dream())
    return 0


def cmd_test_tool(orch: Orchestrator, args: argparse.Namespace) -> int:
    del orch
    path = Path(args.yaml)
    yaml_text = path.read_text(encoding="utf-8")
    script = ""
    if args.script:
        script = Path(args.script).read_text(encoding="utf-8")
    elif path.with_suffix(".sh").is_file():
        script = path.with_suffix(".sh").read_text(encoding="utf-8")
    result = LearnLab.shared().test_tool_install(yaml_text=yaml_text, install_script=script)
    _emit(result)
    return 0 if result.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Thin orchestrator client")
    p.add_argument("--root", type=Path, default=ROOT, help="Project root (auto skills/tools)")
    sub = p.add_subparsers(dest="cmd", required=False)

    s = sub.add_parser("list-skills", help="List skill catalog")
    s.set_defaults(func=cmd_list_skills)

    s = sub.add_parser("list-roles", help="List roles (manager, analyzer, specialists)")
    s.set_defaults(func=cmd_list_roles)

    s = sub.add_parser("project", help="Plan and run objectives with the scheduler (dependencies, replan)")
    s.add_argument("prompt")
    s.add_argument("--session", default=None, help="project id")
    s.add_argument("--timeout", type=float, default=None)
    s.set_defaults(func=cmd_project)

    s = sub.add_parser("memory-health", help="Check the Cognee memory sidecar")
    s.set_defaults(func=cmd_memory_health)

    s = sub.add_parser("dream", help="Run one dream cycle (gists, playbooks, proposals)")
    s.set_defaults(func=cmd_dream)

    s = sub.add_parser("list-tools", help="List tools/catalog CLIs")
    s.set_defaults(func=cmd_list_tools)

    s = sub.add_parser("suggest-tools", help="Draft a catalog tool YAML (LLM)")
    s.add_argument("prompt")
    s.set_defaults(func=cmd_suggest_tools)

    s = sub.add_parser("suggest-skills", help="Recommend existing skills for a prompt")
    s.add_argument("prompt")
    s.set_defaults(func=cmd_suggest_skills)

    s = sub.add_parser("draft-skill", help="Scaffold a new skill (LLM + linter)")
    s.add_argument("prompt")
    s.set_defaults(func=cmd_draft_skill)

    s = sub.add_parser("lint-skill", help="Lint an existing skill directory on disk")
    s.add_argument("skill", help="Skill name (under skills/) or path to skill dir")
    s.set_defaults(func=cmd_lint_skill)

    s = sub.add_parser("plan", help="Plan only (no execution)")
    s.add_argument("prompt")
    s.add_argument("--mode", choices=("project", "job"), default="project")
    s.add_argument("--role", action="append", default=[])
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("run", help="Run a role (or --project: manager + specialists + analyzer)")
    s.add_argument("prompt")
    s.add_argument("--role", action="append", default=[])
    s.add_argument("--skill", action="append", default=[])
    s.add_argument("--project", action="store_true", help="Run the whole crew")
    s.add_argument("--session", default="demo", help="project id / sandbox key")
    s.add_argument("--remove", action="store_true", help="Remove the sandbox afterwards")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("test-tool", help="Install/verify a catalog YAML in learn-lab")
    s.add_argument("yaml", help="Path to tool YAML")
    s.add_argument("--script", help="Optional install .sh")
    s.set_defaults(func=cmd_test_tool)

    return p


def _subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _subcommand_names(parser: argparse.ArgumentParser) -> list[str]:
    action = _subparsers_action(parser)
    return sorted(action.choices) if action else []


def _emit_commands(parser: argparse.ArgumentParser) -> None:
    action = _subparsers_action(parser)
    logger.info("Commands:")
    if action is not None:
        for choice in action._choices_actions:
            logger.info("  %-16s %s", choice.dest, choice.help or "")
    logger.info("")
    logger.info("Meta: help | commands | ? | exit | quit")
    logger.info(
        "Tab completes; double-Tab lists matches. Ctrl+C stops learn-lab (if any) and exits."
    )


def _setup_readline(completions: list[str]) -> None:
    matches: list[str] = []

    def completer(text: str, state: int) -> str | None:
        nonlocal matches
        if state == 0:
            line = readline.get_line_buffer()
            # Complete command name at start of line; otherwise no file noise.
            if not line or line.strip() == text:
                matches = [c for c in completions if c.startswith(text)]
            else:
                matches = []
        return matches[state] if state < len(matches) else None

    readline.set_completer(completer)
    readline.set_completer_delims(" \t\n")
    # bash-like: Tab completes, double-Tab lists
    readline.parse_and_bind("tab: complete")
    try:
        readline.parse_and_bind("set show-all-if-ambiguous on")
        readline.parse_and_bind("set completion-display-width 0")
    except Exception:
        pass


def run_console(orch: Orchestrator, parser: argparse.ArgumentParser) -> int:
    commands = _subcommand_names(parser)
    _setup_readline([*commands, *META_COMMANDS])

    logger.info("Orchestrator console. Type help for commands.\n")
    _emit_commands(parser)

    while True:
        try:
            line = input(PROMPT)
        except KeyboardInterrupt:
            _on_keyboard_interrupt("Interrupted — exiting.")
            return 0
        except EOFError:
            logger.info("")
            return 0

        line = line.strip()
        if not line:
            continue

        try:
            tokens = shlex.split(line)
        except ValueError as exc:
            logger.error("parse error: %s", exc)
            continue

        head = tokens[0]
        if head in ("exit", "quit"):
            return 0
        if head in ("help", "commands", "?"):
            if len(tokens) == 1:
                _emit_commands(parser)
            else:
                try:
                    parser.parse_args([tokens[1], "--help"])
                except SystemExit:
                    pass
            continue

        try:
            args = parser.parse_args(tokens)
        except SystemExit:
            # argparse already printed usage / help
            continue

        if not getattr(args, "cmd", None):
            _emit_commands(parser)
            continue

        try:
            code = int(args.func(orch, args))
            if code != 0:
                logger.info("(exit %s)", code)
        except KeyboardInterrupt:
            _on_keyboard_interrupt("Interrupted — command cancelled.")
        except Exception as exc:
            logger.error("error: %s", exc)


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    load_dotenv(ROOT / ".env")
    parser = build_parser()
    args = parser.parse_args(argv)
    orch = Orchestrator(root=args.root)
    if not getattr(args, "cmd", None):
        return run_console(orch, parser)
    try:
        return int(args.func(orch, args))
    except KeyboardInterrupt:
        _on_keyboard_interrupt("Interrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
