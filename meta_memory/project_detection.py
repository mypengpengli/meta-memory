"""Project inference and persistent directory-to-project bindings."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig, slug


@dataclass(frozen=True)
class ProjectContext:
    name: str
    project_id: str
    root: Path

    @property
    def workspace_id(self) -> str:
        return f"project:{self.project_id}"


def _git_root(start: Path) -> Path | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            check=True, capture_output=True, text=True, timeout=2,
        )
        return Path(completed.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        return None


def project_root(start: str | Path | None = None) -> Path:
    directory = Path(start or Path.cwd()).expanduser().resolve()
    return _git_root(directory) or directory


def resolve_project(config: AppConfig, requested: str = "auto", start: str | Path | None = None) -> ProjectContext:
    root = project_root(start)
    explicit = str(requested or "auto").strip()
    if explicit and explicit.casefold() != "auto":
        return ProjectContext(explicit, slug(explicit, config.default_project), root)
    bound = config.projects.get(str(root))
    name = bound or (root.name if root.name else config.default_project)
    return ProjectContext(name, slug(name, config.default_project), root)


def bind_project(config: AppConfig, name: str, start: str | Path | None = None) -> ProjectContext:
    context = resolve_project(config, name, start)
    config.projects[str(context.root)] = context.project_id
    return context
