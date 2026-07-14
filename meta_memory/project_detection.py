"""Project inference and persistent directory-to-project bindings."""
from __future__ import annotations

import hashlib
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


def _git_remote_identity(root: Path) -> str:
    """Return a normalized local Git identity without contacting a remote.

    A path hash keeps unrelated unbound folders separate, but it changes after
    a clone moves to another computer.  A configured ``origin`` is stable
    across normal clones, so use it as the auto-project fingerprint whenever
    it is available.  Only its hash is persisted in the workspace id.
    """

    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    value = completed.stdout.strip()
    if not value:
        return ""
    # Treat the common SSH and HTTPS spellings of the same repository alike;
    # this value is never displayed, only hashed below.
    normalized = value.casefold().strip().removesuffix(".git").rstrip("/")
    normalized = normalized.replace("ssh://git@", "").replace("https://", "").replace("http://", "")
    if normalized.startswith("git@") and ":" in normalized:
        host, path = normalized[4:].split(":", 1)
        normalized = f"{host}/{path}"
    return normalized


def _remote_fingerprint(remote_identity: str) -> str:
    """Return the non-reversible identifier used for a Git remote binding."""

    return hashlib.sha256(remote_identity.encode("utf-8")).hexdigest()[:16]


def _remote_binding_key(root: Path, *, remote_identity: str | None = None) -> str | None:
    """Return a portable config key for a Git remote without persisting its URL.

    ``config.projects`` historically used an absolute checkout path as its
    key.  Keep supporting that key, but add this hashed alternative so an
    explicitly bound project survives a backup restore into another clone
    directory.  The remote URL itself (which may contain credentials) is
    never written to the configuration file.
    """

    identity = _git_remote_identity(root) if remote_identity is None else remote_identity
    if not identity:
        return None
    return f"remote:{_remote_fingerprint(identity)}"


def project_root(start: str | Path | None = None) -> Path:
    directory = Path(start or Path.cwd()).expanduser().resolve()
    return _git_root(directory) or directory


def resolve_project(config: AppConfig, requested: str = "auto", start: str | Path | None = None) -> ProjectContext:
    root = project_root(start)
    explicit = str(requested or "auto").strip()
    if explicit and explicit.casefold() != "auto":
        return ProjectContext(explicit, slug(explicit, config.default_project), root)
    bound = config.projects.get(str(root))
    if bound:
        return ProjectContext(bound, slug(bound, config.default_project), root)
    # An unbound project cannot safely be identified by its basename alone:
    # two repositories named `api` in separate paths would otherwise share
    # scoped memory.  Prefer the configured Git remote so a normal clone on a
    # new computer finds the same project memory after a backup restore; use
    # the canonical path only when there is no stable repository identity.
    name = root.name if root.name else config.default_project
    remote_identity = _git_remote_identity(root)
    if remote_identity:
        portable_key = _remote_binding_key(root, remote_identity=remote_identity)
        if portable_key:
            portable_binding = config.projects.get(portable_key)
            if portable_binding:
                return ProjectContext(portable_binding, slug(portable_binding, config.default_project), root)
        # The checkout directory is deliberately not part of this ID: a
        # backup restored on another machine must recognize the same remote
        # repository even when it was cloned into a differently named folder.
        fingerprint = _remote_fingerprint(remote_identity)[:10]
        return ProjectContext(name, f"repo-{fingerprint}", root)
    fingerprint = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:10]
    return ProjectContext(name, f"{slug(name, config.default_project)}-{fingerprint}", root)


def bind_project(config: AppConfig, name: str, start: str | Path | None = None) -> ProjectContext:
    context = resolve_project(config, name, start)
    config.projects[str(context.root)] = context.project_id
    portable_key = _remote_binding_key(context.root)
    if portable_key:
        config.projects[portable_key] = context.project_id
    return context
