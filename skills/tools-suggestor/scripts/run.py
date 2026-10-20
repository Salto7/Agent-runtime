#!/usr/bin/env python3
"""Suggest tools/catalog YAML from argv prompt."""
from __future__ import annotations

import json
import sys

from context import SkillContext
from skill_entry import main, workspace_path


def _prompt() -> str:
    ctx = SkillContext()
    text = (ctx.explicit_command() or "").strip()
    if text:
        return text
    if len(sys.argv) > 1:
        return " ".join(sys.argv[1:]).strip()
    return ""


def _run() -> int:
    prompt = _prompt()
    if not prompt:
        print("usage: provide a tool request as argv", file=sys.stderr)
        return 2

    out_dir = workspace_path() / "learn"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        from orchestrator.learn.authoring import LearnAuthoring

        result = LearnAuthoring.shared().suggest_tool(prompt)
    except Exception as exc:  # noqa: BLE001 — surface to skill stream
        (out_dir / "notes.md").write_text(
            f"# tools-suggestor failed\n\n```\n{exc}\n```\n",
            encoding="utf-8",
        )
        print(f"tools-suggestor error: {exc}", file=sys.stderr)
        return 1

    yaml_text = str(result.get("yaml") or "").strip()
    if yaml_text:
        (out_dir / "tool.yaml").write_text(yaml_text + "\n", encoding="utf-8")
    script = str(result.get("install_script") or "").strip()
    if script:
        (out_dir / "tool.sh").write_text(script + "\n", encoding="utf-8")
    notes = str(result.get("notes") or "").strip() or f"id={result.get('id')}"
    (out_dir / "notes.md").write_text(notes + "\n", encoding="utf-8")
    (out_dir / "result.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(f"tools-suggestor wrote under {out_dir}")
    print(f"id={result.get('id') or ''}")
    return 0 if yaml_text else 1


if __name__ == "__main__":
    main(_run)
