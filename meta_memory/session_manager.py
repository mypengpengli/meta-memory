"""Resolve stable, local session identifiers for host AI agents.

The public runtime intentionally treats sessions as a small implementation
detail.  This module supplies a repeatable external session id when a host
does not provide one, without putting a machine-specific id in the database
configuration.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from .config import AppConfig
from .project_detection import ProjectContext


TERMINAL_SESSION_ENVIRONMENT = (
    "TERM_SESSION_ID",
    "WT_SESSION",
    "TMUX_PANE",
    "STY",
    "SSH_TTY",
)


@dataclass(frozen=True)
class SessionResolution:
    """The externally visible session selected for a single Agent turn."""

    session_id: str
    source: str
    agent_id: str
    project_id: str
    host_key: str = ""
    cache_path: Path | None = None
    reused: bool = False


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return _now(value).isoformat().replace("+00:00", "Z")


def _parse_stamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return _now(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def normalize_agent_id(agent_id: str) -> str:
    """Use a safe, stable prefix while retaining an understandable id."""

    value = str(agent_id or "").strip().casefold()
    return re.sub(r"[^a-z0-9_.-]+", "-", value).strip("-.") or "generic-agent"


def session_cache_dir(config: AppConfig) -> Path:
    """Keep cache state alongside the selected config, not the current cwd."""

    return Path(config.path).expanduser().resolve().parent / "runtime" / "sessions"


def _expiry_hours(config: AppConfig) -> int:
    # AppConfig gains this field with the public Session CLI.  The fallback
    # keeps this module usable while older config files are migrated.
    try:
        return max(1, int(getattr(config, "session_auto_expire_hours", 8)))
    except (TypeError, ValueError):
        return 8


def _host_key(*, source: str, value: str, agent_id: str, project: ProjectContext) -> str:
    payload = {
        "agent_id": normalize_agent_id(agent_id),
        "project_root": str(Path(project.root).expanduser().resolve()),
        "source": source,
        "value": value,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_path(config: AppConfig, host_key: str) -> Path:
    return session_cache_dir(config) / f"{host_key}.json"


def _read_cache(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_cache(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _cache_is_current(
    payload: dict[str, object],
    *,
    host_key: str,
    agent_id: str,
    project: ProjectContext,
    expires_after_hours: int,
    current: datetime,
) -> bool:
    last_seen = _parse_stamp(payload.get("last_seen_at"))
    if last_seen is None or current - last_seen > timedelta(hours=expires_after_hours):
        return False
    return (
        str(payload.get("host_key") or "") == host_key
        and str(payload.get("agent_id") or "") == normalize_agent_id(agent_id)
        and str(payload.get("project_id") or "") == str(project.project_id)
        and str(payload.get("project_root") or "") == str(Path(project.root).expanduser().resolve())
        and bool(str(payload.get("session_id") or "").strip())
    )


def _automatic_source(
    *,
    requested: str,
    agent_id: str,
    project: ProjectContext,
    environ: Mapping[str, str],
    parent_pid: int,
) -> tuple[str, str, str | None]:
    """Return source, source value, and direct host session if available."""

    explicit = str(requested or "").strip()
    if explicit and explicit.casefold() != "auto":
        return "explicit", explicit, explicit

    host_session = str(environ.get("META_MEMORY_HOST_SESSION_ID", "") or "").strip()
    if host_session:
        return "host", host_session, host_session

    for name in TERMINAL_SESSION_ENVIRONMENT:
        value = str(environ.get(name, "") or "").strip()
        if value:
            return "terminal", f"{name}:{value}", None

    return "parent", str(int(parent_pid)), None


def _resolve_cached(
    config: AppConfig,
    *,
    source: str,
    source_value: str,
    agent_id: str,
    project: ProjectContext,
    current: datetime,
    force_new: bool,
) -> SessionResolution:
    normalized_agent = normalize_agent_id(agent_id)
    host_key = _host_key(source=source, value=source_value, agent_id=normalized_agent, project=project)
    path = _cache_path(config, host_key)
    expiry = _expiry_hours(config)
    cached = _read_cache(path)
    if not force_new and cached and _cache_is_current(
        cached,
        host_key=host_key,
        agent_id=normalized_agent,
        project=project,
        expires_after_hours=expiry,
        current=current,
    ):
        cached["last_seen_at"] = _stamp(current)
        cached["expires_after_hours"] = expiry
        _write_cache(path, cached)
        return SessionResolution(
            session_id=str(cached["session_id"]),
            source=source,
            agent_id=normalized_agent,
            project_id=str(project.project_id),
            host_key=host_key,
            cache_path=path,
            reused=True,
        )

    session_id = f"{normalized_agent}:{uuid.uuid4()}"
    _write_cache(
        path,
        {
            "session_id": session_id,
            "agent_id": normalized_agent,
            "project_id": str(project.project_id),
            "project_root": str(Path(project.root).expanduser().resolve()),
            "host_key": host_key,
            "source": source,
            "created_at": _stamp(current),
            "last_seen_at": _stamp(current),
            "expires_after_hours": expiry,
        },
    )
    return SessionResolution(
        session_id=session_id,
        source=source,
        agent_id=normalized_agent,
        project_id=str(project.project_id),
        host_key=host_key,
        cache_path=path,
        reused=False,
    )


def resolve_session(
    config: AppConfig,
    *,
    requested: str = "auto",
    agent_id: str = "generic-agent",
    project: ProjectContext,
    environ: Mapping[str, str] | None = None,
    parent_pid: int | None = None,
    now: datetime | None = None,
) -> SessionResolution:
    """Resolve a stable session using the documented host-to-local priority."""

    environment = os.environ if environ is None else environ
    current = _now(now)
    source, source_value, direct_session = _automatic_source(
        requested=requested,
        agent_id=agent_id,
        project=project,
        environ=environment,
        parent_pid=os.getppid() if parent_pid is None else parent_pid,
    )
    normalized_agent = normalize_agent_id(agent_id)
    if direct_session is not None:
        return SessionResolution(
            session_id=direct_session,
            source=source,
            agent_id=normalized_agent,
            project_id=str(project.project_id),
        )
    return _resolve_cached(
        config,
        source=source,
        source_value=source_value,
        agent_id=normalized_agent,
        project=project,
        current=current,
        force_new=False,
    )


def new_session(
    config: AppConfig,
    *,
    requested: str = "auto",
    agent_id: str = "generic-agent",
    project: ProjectContext,
    environ: Mapping[str, str] | None = None,
    parent_pid: int | None = None,
    now: datetime | None = None,
) -> SessionResolution:
    """Rotate a locally-derived automatic session for the current host key."""

    environment = os.environ if environ is None else environ
    current = _now(now)
    source, source_value, direct_session = _automatic_source(
        requested=requested,
        agent_id=agent_id,
        project=project,
        environ=environment,
        parent_pid=os.getppid() if parent_pid is None else parent_pid,
    )
    normalized_agent = normalize_agent_id(agent_id)
    # A host-provided conversation id is authoritative; creating a local
    # replacement would make host/tool transcripts disagree.
    if direct_session is not None:
        return SessionResolution(
            session_id=direct_session,
            source=source,
            agent_id=normalized_agent,
            project_id=str(project.project_id),
        )
    return _resolve_cached(
        config,
        source=source,
        source_value=source_value,
        agent_id=normalized_agent,
        project=project,
        current=current,
        force_new=True,
    )


def close_session(
    config: AppConfig,
    *,
    requested: str = "auto",
    agent_id: str = "generic-agent",
    project: ProjectContext,
    environ: Mapping[str, str] | None = None,
    parent_pid: int | None = None,
) -> SessionResolution | None:
    """Forget local cache state; runtime callers may also end the DB session."""

    environment = os.environ if environ is None else environ
    source, source_value, direct_session = _automatic_source(
        requested=requested,
        agent_id=agent_id,
        project=project,
        environ=environment,
        parent_pid=os.getppid() if parent_pid is None else parent_pid,
    )
    normalized_agent = normalize_agent_id(agent_id)
    if direct_session is not None:
        return SessionResolution(
            session_id=direct_session,
            source=source,
            agent_id=normalized_agent,
            project_id=str(project.project_id),
        )
    host_key = _host_key(source=source, value=source_value, agent_id=normalized_agent, project=project)
    path = _cache_path(config, host_key)
    cached = _read_cache(path)
    if not cached:
        return None
    try:
        path.unlink()
    except OSError:
        return None
    return SessionResolution(
        session_id=str(cached.get("session_id") or ""),
        source=source,
        agent_id=normalized_agent,
        project_id=str(project.project_id),
        host_key=host_key,
        cache_path=path,
        reused=True,
    )
