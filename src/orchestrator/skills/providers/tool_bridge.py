"""Claude Agent Skill tools are the crew surface.

``Bash`` / ``Read`` / ``Write`` / ``Edit`` map onto progressive + shell tools.
Known Peon / CrewAI tool names pass through unchanged.
"""

from __future__ import annotations

from typing import Sequence

# Token before "(" , lowercased, → canonical crew tool names.
CLAUDE_TOOL_BRIDGE: dict[str, tuple[str, ...]] = {
    "bash": ("bash", "run_skill_script"),
    "read": ("read_skill_resource", "load_skill"),
    "write": ("bash",),
    "edit": ("bash",),
}

PROGRESSIVE_TOOLS: tuple[str, ...] = (
    "load_skill",
    "read_skill_resource",
    "run_skill_script",
)


def tool_key(raw: str) -> str:
    text = str(raw or "").strip()
    paren = text.find("(")
    if paren > 0:
        text = text[:paren]
    return text.strip().lower()


def _unique(names: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        key = str(name or "").strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return tuple(out)


def classify_claude_tools(raw_tools: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(crew_tools, unmapped_raw)``."""
    from orchestrator.crew.tools.catalog import canonicalize_crew_tools, known_tool_names

    crew: list[str] = list(PROGRESSIVE_TOOLS)
    unknown: list[str] = []
    known = {n.strip().lower() for n in known_tool_names()}
    for raw in raw_tools:
        token = str(raw or "").strip()
        if not token:
            continue
        mapped = CLAUDE_TOOL_BRIDGE.get(tool_key(token))
        if mapped:
            crew.extend(mapped)
            continue
        key = tool_key(token)
        if key in known or key in PROGRESSIVE_TOOLS:
            crew.extend(canonicalize_crew_tools([token]) or (key,))
            continue
        unknown.append(token)
    return _unique(crew), _unique(unknown)
