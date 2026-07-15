"""Project inference and persistent directory-to-project bindings."""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .config import AppConfig, slug


@dataclass(frozen=True)
class ProjectContext:
    name: str
    project_id: str
    root: Path
    # Computed while the project is resolved so before/audit/status can reuse
    # it without launching git again in the same user turn.
    remote_identity: str = ""
    repository_fingerprint: str = ""

    @property
    def workspace_id(self) -> str:
        return f"project:{self.project_id}"


_REMOTE_CACHE: dict[str, tuple[tuple[int, int] | None, str]] = {}
_MAX_REMOTE_CACHE: Final[int] = 128


def _git_metadata_path(root: Path) -> Path | None:
    """Locate the Git config for a normal checkout or linked worktree."""

    marker = root / ".git"
    if marker.is_dir():
        return marker / "config"
    if marker.is_file():
        try:
            line = marker.read_text(encoding="utf-8", errors="replace").splitlines()[0]
            if line.casefold().startswith("gitdir:"):
                value = line.split(":", 1)[1].strip()
                git_dir = Path(value)
                if not git_dir.is_absolute():
                    git_dir = (marker.parent / git_dir).resolve()
                return git_dir / "config"
        except (OSError, IndexError):
            return None
    return None


def _git_metadata_signature(root: Path) -> tuple[int, int] | None:
    path = _git_metadata_path(root)
    if not path:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return (int(stat.st_mtime_ns), int(stat.st_size))


def _nearest_git_root(start: Path) -> Path | None:
    """Avoid a git subprocess for the common checkout/worktree layout."""

    current = start if start.is_dir() else start.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate.resolve()
    return None


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

    resolved = root.expanduser().resolve()
    key = str(resolved)
    signature = _git_metadata_signature(resolved)
    cached = _REMOTE_CACHE.get(key)
    if cached and cached[0] == signature:
        return cached[1]
    try:
        completed = subprocess.run(
            ["git", "-C", str(resolved), "config", "--get", "remote.origin.url"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        value = ""
        if len(_REMOTE_CACHE) >= _MAX_REMOTE_CACHE:
            _REMOTE_CACHE.pop(next(iter(_REMOTE_CACHE)))
        _REMOTE_CACHE[key] = (signature, value)
        return value
    value = completed.stdout.strip()
    if not value:
        if len(_REMOTE_CACHE) >= _MAX_REMOTE_CACHE:
            _REMOTE_CACHE.pop(next(iter(_REMOTE_CACHE)))
        _REMOTE_CACHE[key] = (signature, "")
        return ""
    # Treat the common SSH and HTTPS spellings of the same repository alike;
    # this value is never displayed, only hashed below.
    normalized = value.casefold().strip().removesuffix(".git").rstrip("/")
    normalized = normalized.replace("ssh://git@", "").replace("https://", "").replace("http://", "")
    if normalized.startswith("git@") and ":" in normalized:
        host, path = normalized[4:].split(":", 1)
        normalized = f"{host}/{path}"
    if len(_REMOTE_CACHE) >= _MAX_REMOTE_CACHE:
        _REMOTE_CACHE.pop(next(iter(_REMOTE_CACHE)))
    _REMOTE_CACHE[key] = (signature, normalized)
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
    return _nearest_git_root(directory) or _git_root(directory) or directory


def _context(name: str, project_id: str, root: Path, remote_identity: str) -> ProjectContext:
    material = remote_identity or str(root.expanduser().resolve())
    return ProjectContext(
        name,
        project_id,
        root,
        remote_identity=remote_identity,
        repository_fingerprint=hashlib.sha256(material.encode("utf-8")).hexdigest()[:16],
    )


def resolve_project(config: AppConfig, requested: str = "auto", start: str | Path | None = None) -> ProjectContext:
    root = project_root(start)
    # Read Git metadata exactly once.  The returned context transports this
    # value through runtime audit/status instead of re-running `git config`.
    remote_identity = _git_remote_identity(root)
    explicit = str(requested or "auto").strip()
    if explicit and explicit.casefold() != "auto":
        return _context(explicit, slug(explicit, config.default_project), root, remote_identity)
    bound = config.projects.get(str(root))
    if bound:
        return _context(bound, slug(bound, config.default_project), root, remote_identity)
    # An unbound project cannot safely be identified by its basename alone:
    # two repositories named `api` in separate paths would otherwise share
    # scoped memory.  Prefer the configured Git remote so a normal clone on a
    # new computer finds the same project memory after a backup restore; use
    # the canonical path only when there is no stable repository identity.
    name = root.name if root.name else config.default_project
    if remote_identity:
        portable_key = _remote_binding_key(root, remote_identity=remote_identity)
        if portable_key:
            portable_binding = config.projects.get(portable_key)
            if portable_binding:
                return _context(portable_binding, slug(portable_binding, config.default_project), root, remote_identity)
        # The checkout directory is deliberately not part of this ID: a
        # backup restored on another machine must recognize the same remote
        # repository even when it was cloned into a differently named folder.
        fingerprint = _remote_fingerprint(remote_identity)[:10]
        return _context(name, f"repo-{fingerprint}", root, remote_identity)
    fingerprint = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:10]
    return _context(name, f"{slug(name, config.default_project)}-{fingerprint}", root, remote_identity)


def bind_project(config: AppConfig, name: str, start: str | Path | None = None) -> ProjectContext:
    context = resolve_project(config, name, start)
    config.projects[str(context.root)] = context.project_id
    portable_key = _remote_binding_key(context.root, remote_identity=context.remote_identity)
    if portable_key:
        config.projects[portable_key] = context.project_id
    return context
