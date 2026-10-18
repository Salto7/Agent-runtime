"""Library CLI entry (same surface as examples/thin_client.py)."""

from __future__ import annotations

from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    # Reuse the thin client implementation.
    root = Path(__file__).resolve().parents[2]
    examples = root / "examples" / "thin_client.py"
    # When installed editable, examples live next to src/
    if not examples.is_file():
        examples = Path.cwd() / "examples" / "thin_client.py"
    if examples.is_file():
        import importlib.util

        spec = importlib.util.spec_from_file_location("orchestrator_thin_client", examples)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return int(mod.main(argv))
    # Fallback: minimal help
    from orchestrator import Orchestrator

    orch = Orchestrator()
    print("skills:", len(orch.list_skills()))
    print("tools:", len(orch.list_tools()))
    print("Use: python examples/thin_client.py --help")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
