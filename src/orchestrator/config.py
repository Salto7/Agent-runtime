"""Platform-agnostic runtime configuration (pydantic-settings from env — no Django)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated, Any

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

import orchestrator as _pkg

from orchestrator.prompts import FINDINGS_GUIDANCE, INSTALL_CASCADE

logger = logging.getLogger(__name__)

# Engagement default; host may override via configure(...) / ORCHESTRATOR_SYSTEM_PREAMBLE.
_DEFAULT_SYSTEM_PREAMBLE = (
    "You are a CrewAI agent working an authorized project.\n"
    "Execute selected skills from your role's skill group; skills do not spawn agents.\n"
    "Use only bound tools. Technical and engagement-pm agents may spawn_work.\n"
    f"Call provision_cli before missing CLIs ({INSTALL_CASCADE}).\n"
    "Use bash for sandbox shell.\n"
    "Scope rules are optional: when the project has them (see roe_status), never expand them, stay in scope and call\n"
    "assert_in_scope before networked probes; with none set, scope is unrestricted.\n"
    "Obey OPERATOR INSTRUCTION / FOLLOW-UP messages when they appear.\n"
    f"{FINDINGS_GUIDANCE}"
)
_DEFAULT_LLM_PROVIDER = "openrouter"
_DEFAULT_LLM_MODULE = "litellm"
_DEFAULT_LITELLM_MODEL = "openrouter/openai/gpt-5.6-luna"
_DEFAULT_LLM_PROXY_URL = "http://litellm:4000/v1"
# Public SoT for upstream provider → env var (also used by native apps bootstrap).
LLM_PROVIDER_KEY_ENV = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def _env_names(*names: str) -> Any:
    """Environment variable names for a field (keyword construction still uses the field name)."""
    return AliasChoices(*names)


_PATH_FIELDS = (
    "tools_catalog_dir",
    "workspaces_dir",
    "platform_workspace_dir",
    "skills_dir",
    "skills_imported_dir",
    "mcps_dir",
)


class RuntimeConfig(BaseSettings):
    """Paths, LLM, agent, and sandbox settings for one process.

    Values come from keyword arguments, then environment variables (empty values
    are ignored). Relative paths resolve against ``config_root``.
    """

    model_config = SettingsConfigDict(
        env_ignore_empty=True,
        populate_by_name=True,
        extra="ignore",
    )

    config_root: Path = Field(
        default_factory=Path.cwd, validation_alias=_env_names("ORCHESTRATOR_CONFIG_ROOT"), exclude=True
    )

    tools_catalog_dir: Path = Path("tools/catalog")
    workspaces_dir: Path = Field(
        Path("workspaces"), validation_alias=_env_names("PROJECT_WORKSPACES_DIR")
    )
    # Always ``<workspaces_dir>/platform`` unless passed explicitly.
    platform_workspace_dir: Path | None = Field(
        None, validation_alias=_env_names("ORCHESTRATOR_PLATFORM_WORKSPACE_DIR")
    )

    llm_provider: str = _DEFAULT_LLM_PROVIDER
    llm_module: str = _DEFAULT_LLM_MODULE
    litellm_model: str = Field(
        _DEFAULT_LITELLM_MODEL, validation_alias=_env_names("LITELLM_MODEL", "LLM_MODEL")
    )
    # Provider key first (see ``from_env``), then these in order.
    litellm_api_key: str = Field(
        "",
        validation_alias=_env_names("LITELLM_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"),
    )
    litellm_api_base: str | None = None
    llm_temperature: float = 0.0
    llm_max_tokens: int | None = None
    # Optional OpenAI-compatible proxy exposure (Compose profile llm-proxy).
    llm_proxy_enabled: bool = False
    llm_proxy_url: str = _DEFAULT_LLM_PROXY_URL

    agent_max_iterations: int = 40
    agent_max_failure_replans: int = 2
    agent_max_subagents: int = 8
    agent_max_subagent_depth: int = 2
    agent_runtime_enabled: bool = True
    # When RoleSpec.reasoning is true — CrewAI PlanningConfig knobs.
    crew_reasoning_effort: str = "low"
    crew_reasoning_max_attempts: int = 1
    # Job/crew orchestration module (crewai only on CrewAI).
    agent_module: str = "crewai"
    skills_dir: Path = Path("skills")
    # Deprecated: defaults to skills_dir. Kept so old env / mounts still resolve.
    skills_imported_dir: Path | None = None
    skills_extra_dirs: Annotated[tuple[Path, ...], NoDecode] = ()
    mcps_dir: Path = Path("mcps")

    sandbox_image: str = "orchestrator-sandbox:local"
    sandbox_prefix: str = Field(
        "orchestrator-project", validation_alias=_env_names("PROJECT_SANDBOX_PREFIX", "SANDBOX_PREFIX")
    )
    sandbox_remove_on_exit: bool = False

    system_preamble: str = Field(
        _DEFAULT_SYSTEM_PREAMBLE, validation_alias=_env_names("ORCHESTRATOR_SYSTEM_PREAMBLE")
    )

    @field_validator("skills_extra_dirs", mode="before")
    @classmethod
    def _split_dirs(cls, value: Any) -> Any:
        if isinstance(value, str):
            return tuple(p.strip() for p in value.replace(";", ":").split(":") if p.strip())
        return value

    @field_validator("crew_reasoning_effort", "agent_module", mode="after")
    @classmethod
    def _lower(cls, value: str) -> str:
        return value.strip().lower()

    @model_validator(mode="after")
    def _resolve_paths(self) -> RuntimeConfig:
        root = self.config_root.expanduser().resolve()

        def resolve(path: Path) -> Path:
            path = path.expanduser()
            return (path if path.is_absolute() else root / path).resolve()

        for name in _PATH_FIELDS:
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, resolve(value))
        if self.skills_imported_dir is None:
            object.__setattr__(self, "skills_imported_dir", self.skills_dir)
        if self.platform_workspace_dir is None:
            object.__setattr__(self, "platform_workspace_dir", self.workspaces_dir / "platform")
        object.__setattr__(
            self, "skills_extra_dirs", tuple(resolve(p) for p in self.skills_extra_dirs)
        )
        return self

    def ensure_dirs(self) -> None:
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        if self.skills_imported_dir.resolve() != self.skills_dir.resolve():
            self.skills_imported_dir.mkdir(parents=True, exist_ok=True)
        self.mcps_dir.mkdir(parents=True, exist_ok=True)
        self.tools_catalog_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        self.platform_workspace_dir.mkdir(parents=True, exist_ok=True)

    def apply_env(self) -> None:
        """Export path hints used by catalog / docker exec helpers."""
        os.environ["SKILLS_DIR"] = str(self.skills_dir)
        # Alias: imports share skills/. Extra distinct path only if configured.
        os.environ["SKILLS_IMPORTED_DIR"] = str(self.skills_imported_dir)
        os.environ["SKILLS_EXTRA_DIRS"] = ":".join(
            str(path) for path in self.skills_extra_dirs
        )
        os.environ["MCPS_DIR"] = str(self.mcps_dir)
        os.environ["TOOLS_CATALOG_DIR"] = str(self.tools_catalog_dir)
        os.environ["PROJECT_WORKSPACES_DIR"] = str(self.workspaces_dir)
        os.environ["PLATFORM_WORKSPACE_DIR"] = str(self.platform_workspace_dir)
        os.environ["AGENT_MODULE"] = (self.agent_module or "crewai").strip().lower()
        if self.litellm_api_key:
            provider = (self.llm_provider or "").strip().lower()
            key_env = LLM_PROVIDER_KEY_ENV.get(provider, "LITELLM_API_KEY")
            os.environ.setdefault(key_env, self.litellm_api_key)
            os.environ.setdefault("LITELLM_API_KEY", self.litellm_api_key)

    @classmethod
    def from_env(cls, *, root: Path | None = None) -> RuntimeConfig:
        """Settings from the environment; relative defaults resolve under ``root``."""
        kwargs: dict[str, Any] = {"config_root": (root or Path.cwd()).resolve()}
        provider = (os.environ.get("LLM_PROVIDER") or _DEFAULT_LLM_PROVIDER).strip()
        provider_key = (os.environ.get(LLM_PROVIDER_KEY_ENV.get(provider, "")) or "").strip()
        if provider_key:
            kwargs["litellm_api_key"] = provider_key
        return cls(**kwargs)

    def merge(self, **overrides: object) -> RuntimeConfig:
        # re-validate (not model_copy): derived paths and validators must see the overrides
        data = self.model_dump()
        # derived defaults follow an override of what they derive from (unless set explicitly)
        if "workspaces_dir" in overrides and "platform_workspace_dir" not in overrides:
            data.pop("platform_workspace_dir", None)
        if "skills_dir" in overrides and "skills_imported_dir" not in overrides:
            data.pop("skills_imported_dir", None)
        data.setdefault("config_root", self.config_root)
        return type(self).model_validate({**data, **overrides})


def load_env_file(root: Path | str | None = None) -> Path | None:
    """Load ``<root>/.env`` into ``os.environ`` (never overriding real environment variables).

    Called by ``bootstrap()`` / ``get_config()`` so a project's ``.env`` configures the library, the CLI and
    the Dramatiq workers alike. Returns the file that was loaded, if any.
    """
    path = Path(root or Path.cwd()).expanduser().resolve() / ".env"
    if not path.is_file():
        return None
    try:
        from dotenv import load_dotenv

        load_dotenv(path, override=False)
    except Exception as exc:  # noqa: BLE001 — a bad .env must not stop the process
        logger.warning("could not load %s: %s", path, exc)
        return None
    return path


def discover_root(start: Path | str | None = None) -> Path:
    """Find a project root that contains ``skills/`` and ``tools/catalog/`` (``ORCHESTRATOR_CONFIG_ROOT`` wins)."""
    explicit = (os.environ.get("ORCHESTRATOR_CONFIG_ROOT") or "").strip()
    if explicit and start is None:
        return Path(explicit).expanduser().resolve()
    here = Path(start or Path.cwd()).resolve()
    candidates = [here, *here.parents]
    try:
        pkg = Path(_pkg.__file__).resolve()
        # In-tree: <repo>/orchestrator/__init__.py → parents[1]
        # src-layout: <repo>/src/orchestrator/__init__.py → parents[2]
        candidates.extend([pkg.parents[1], pkg.parents[2]])
    except Exception:
        pass
    for path in candidates:
        if (path / "skills").is_dir() and (path / "tools" / "catalog").is_dir():
            return path
    return here


_config: RuntimeConfig | None = None


def get_config() -> RuntimeConfig:
    global _config
    if _config is None:
        root = discover_root()
        load_env_file(root)
        _config = RuntimeConfig.from_env(root=root)
        _config.apply_env()
    return _config


def configure(cfg: RuntimeConfig | None = None, **overrides: object) -> RuntimeConfig:
    """Set process-wide config. Call once at startup from the host app."""
    global _config
    base = cfg or _config or RuntimeConfig.from_env()
    if overrides:
        base = base.merge(**overrides)
    base.ensure_dirs()
    base.apply_env()
    _config = base
    try:
        # circular: llm / ToolCatalog → get_config
        from orchestrator.crew.roles.registry import RoleRegistry
        from orchestrator.llm import reset_llm_provider
        from orchestrator.skills.registry import SkillRegistry
        from orchestrator.tools.catalog.catalog import ToolCatalog

        from orchestrator.catalog import PlatformCatalog
        from orchestrator.catalog.store import reset_store

        reset_llm_provider()
        PlatformCatalog.set_shared(None)
        reset_store()
        RoleRegistry.reset_shared()
        ToolCatalog.reset_shared()
        SkillRegistry.reset_shared()
    except Exception as exc:
        logger.warning("resetting LLM provider failed: %s", exc, exc_info=True)
    return _config


def bootstrap(root: Path | str | None = None, **overrides: object) -> RuntimeConfig:
    """Auto-configure from env + a discovered project root (zero-setup entry)."""
    base = discover_root(root)
    load_env_file(base)
    cfg = RuntimeConfig.from_env(root=base)
    if overrides:
        cfg = cfg.merge(**overrides)
    return configure(cfg)


