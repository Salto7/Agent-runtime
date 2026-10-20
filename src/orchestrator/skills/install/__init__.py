"""Install skill packs into the imported skills root."""

from orchestrator.skills.install.dragdrop import (
    install_directory,
    install_relative_files,
    install_zip,
)
from orchestrator.skills.install.openskills import install_openskills
from orchestrator.skills.install.sources import install_skill_source

__all__ = [
    "install_directory",
    "install_openskills",
    "install_relative_files",
    "install_skill_source",
    "install_zip",
]
