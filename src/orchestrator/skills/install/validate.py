"""Install-time pack check. Discovery does not require this package."""

from __future__ import annotations

from pathlib import Path


def validate_pack(skill_dir: Path) -> list[str]:
    """Return skills-ref errors. Raise when the checker is not installed."""
    try:
        from skills_ref import validate
    except ImportError as exc:
        raise RuntimeError(
            "skills-ref is required to install skill packs (pip install skills-ref)"
        ) from exc
    errors = validate(Path(skill_dir))
    if not errors:
        return []
    if isinstance(errors, str):
        return [errors]
    return [str(item) for item in errors if str(item).strip()]
