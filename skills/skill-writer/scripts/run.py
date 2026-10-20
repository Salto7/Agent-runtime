#!/usr/bin/env python3
"""Draft a Claude Agent Skill from argv prompt."""
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
        print("usage: provide a skill request as argv", file=sys.stderr)
        return 2

    out_root = workspace_path() / "learn" / "skill"
    out_root.mkdir(parents=True, exist_ok=True)

    try:
        from orchestrator.learn.authoring import LearnAuthoring

        result = LearnAuthoring.shared().write_skill(prompt)
    except Exception as exc:  # noqa: BLE001
        (out_root / "notes.md").write_text(
            f"# skill-writer failed\n\n```\n{exc}\n```\n",
            encoding="utf-8",
        )
        print(f"skill-writer error: {exc}", file=sys.stderr)
        return 1

    name = str(result.get("name") or "new-skill").strip()
    skill_dir = out_root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = str(result.get("skill_md") or "").strip()
    if md:
        (skill_dir / "SKILL.md").write_text(md + "\n", encoding="utf-8")
    files = result.get("files") or {}
    if isinstance(files, dict):
        for rel, body in files.items():
            path = skill_dir / str(rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(body or "").rstrip() + "\n", encoding="utf-8")
    notes = str(result.get("notes") or "").strip()
    lint = result.get("lint") or {}
    (out_root / "notes.md").write_text(
        (notes or f"skill={name}")
        + "\n\n## Lint\n\n"
        + json.dumps(lint, indent=2)
        + "\n",
        encoding="utf-8",
    )
    (out_root / "result.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(f"skill-writer wrote {skill_dir}")
    print(f"compatible={bool((lint or {}).get('compatible'))}")
    return 0 if md and lint.get("compatible") else 1


if __name__ == "__main__":
    main(_run)
