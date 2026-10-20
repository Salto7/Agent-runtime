"""Learn authoring — tool YAML suggestions and skill scaffolding."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import yaml

from orchestrator.learn.draft import (
    authoring_prompt,
    catalog_summaries,
    filter_catalog,
    finalize_skill_payload,
    propose_missing_tool,
    skill_prompt,
    skill_result_dict,
    tool_replan_human,
    tool_suggest_human,
    tool_suggestion_from_payload,
    valid_skill_name,
)
from orchestrator.skills.provision import SkillLinter
from orchestrator.utils import llm_usage
from orchestrator.utils.llm import require_llm, chat_json
from orchestrator.utils.paths import safe_join
from orchestrator.utils.service import SharedServiceBase

from orchestrator.learn.system_skills import skill_writer_skill, tools_suggestor_skill


class LearnAuthoring(SharedServiceBase):
    """LLM authoring backed by skill-writer / tools-suggestor prompt files.

    Methods are static — no instance state; ``shared()`` remains for DI/tests.
    """

    @staticmethod
    def suggest_tool(prompt: str) -> dict[str, Any]:
        """Return ``id``, ``yaml``, ``install_script``, ``notes`` for a catalog tool."""
        text = (prompt or "").strip()
        if not text:
            raise ValueError("prompt is required")
        require_llm()
        with llm_usage.purpose("authoring_tool_suggest"):
            return tool_suggestion_from_payload(
                chat_json(
                    skill_prompt(tools_suggestor_skill()),
                    tool_suggest_human(text),
                )
            )

    @staticmethod
    def write_skill(
        prompt: str, *, tools: list[str] | None = None
    ) -> dict[str, Any]:
        """Draft a Claude Agent Skill and return the skills-ref summary.

        A catalog CLI becomes ``scripts/run.sh``. A missing CLI still gets
        ``references/INSTALL.md`` from the tools-suggestor recipe.
        """
        text = (prompt or "").strip()
        if not text:
            raise ValueError("prompt is required")
        require_llm()
        catalog = filter_catalog(
            catalog_summaries(),
            tools,
        )

        tool_suggestion, proposed, install_recipes = (
            propose_missing_tool(
                text,
                LearnAuthoring.suggest_tool,
            )
        )

        system = skill_prompt(skill_writer_skill())
        human = authoring_prompt(
            text,
            catalog,
            proposed=proposed,
            tool_recipes=install_recipes or None,
        )

        with llm_usage.purpose("authoring_skill_write"):
            payload = chat_json(system, human)
        if not isinstance(payload, dict):
            raise RuntimeError("skill-writer returned non-object JSON")

        name, skill_md, files, suggested, mode, lint = (
            finalize_skill_payload(
                payload,
                prompt=text,
                proposed=proposed,
                install_recipes=install_recipes,
            )
        )

        if not lint["compatible"]:
            with llm_usage.purpose("authoring_skill_write_repair"):
                repair = chat_json(
                    system,
                    (
                        f"{human}\n\n"
                        f"The previous draft failed skills-ref:\n"
                        f"{yaml.safe_dump(lint['errors'], sort_keys=False)}\n\n"
                        "Return corrected JSON only for the stated authoring mode."
                    ),
                )
            if isinstance(repair, dict):
                try:
                    name, skill_md, files, suggested, mode, lint = (
                        finalize_skill_payload(
                        repair,
                        prompt=text,
                        proposed=proposed,
                        install_recipes=install_recipes,
                        suggested=suggested,
                        )
                    )
                except RuntimeError:
                    pass

        return skill_result_dict(
            name=name,
            skill_md=skill_md,
            files=files,
            notes=str(payload.get("notes") or "").strip(),
            suggested=suggested,
            mode=mode,
            lint=lint,
            tool_suggestion=tool_suggestion,
        )

    @staticmethod
    def replan_tool(
        *,
        prompt: str,
        yaml_text: str,
        install_script: str = "",
        error: str = "",
        feedback: str = "",
    ) -> dict[str, Any]:
        """Revise a tool recipe after a failed learn-lab install test."""
        require_llm()
        with llm_usage.purpose("authoring_tool_replan"):
            return tool_suggestion_from_payload(
                chat_json(
                    skill_prompt(tools_suggestor_skill()),
                    tool_replan_human(
                        prompt=prompt,
                        yaml_text=yaml_text,
                        install_script=install_script,
                        error=error,
                        feedback=feedback,
                    ),
                )
        )

    @staticmethod
    def lint_skill(
        *, name: str, skill_md: str, files: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Lint editable skill content (no LLM)."""
        nm = (name or "").strip().lower()
        md = (skill_md or "").strip()
        file_map = {str(k): str(v) for k, v in (files or {}).items()}
        if not valid_skill_name(nm):
            raise ValueError(f"invalid skill name {nm!r}")
        if not md:
            raise ValueError("skill_md is required")
        with tempfile.TemporaryDirectory(prefix=f"orchestrator-lint-{nm}-") as tmp:
            skill_dir = Path(tmp) / nm
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(md + "\n", encoding="utf-8")
            for rel, body in file_map.items():
                path = safe_join(skill_dir, rel)
                if path is None:
                    issue = {"level": "error", "code": "unsafe_path", "message": f"unsafe file path {rel!r}"}
                    return {"compatible": False, "skill_name": nm, "skill_dir": str(skill_dir),
                            "issues": [issue], "errors": [issue], "warnings": []}
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(body).rstrip() + "\n", encoding="utf-8")
            return SkillLinter.check(skill_dir)
