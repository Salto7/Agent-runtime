"""orchestrator — litellm + Docker sandbox agent orchestration library."""

from orchestrator.api import Orchestrator, OrchestratorResult, PlanResult, run
from orchestrator.config import RuntimeConfig, bootstrap, configure, discover_root, get_config
from orchestrator.learn import LearnAuthoring, LearnLab

__all__ = [
    "LearnAuthoring",
    "LearnLab",
    "Orchestrator",
    "OrchestratorResult",
    "PlanResult",
    "RuntimeConfig",
    "bootstrap",
    "configure",
    "discover_root",
    "get_config",
    "run",
]

__version__ = "0.1.0"
