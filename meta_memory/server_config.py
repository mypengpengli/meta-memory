"""Create and extend the non-secret server Agent binding file."""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Iterable


DEFAULT_REMOTE_PERMISSIONS = [
    "turns", "status", "read", "record", "remember", "feedback", "proposals",
    "shared", "assets", "maps", "spatial",
]


def _values(items: Iterable[str], name: str) -> list[str]:
    values = sorted({str(item or "").strip() for item in items if str(item or "").strip()})
    if not values:
        raise ValueError(f"at least one {name} is required")
    return values


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes((json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def write_agent_binding(
    output: str | Path,
    *,
    profile_id: str,
    agent_id: str,
    token_env: str,
    workspaces: Iterable[str],
    subject_ids: Iterable[str],
    audiences: Iterable[str] = (),
    replace_agent: bool = False,
) -> dict[str, Any]:
    """Add one Agent principal while preserving other entries in the file."""

    path = Path(output).expanduser().resolve()
    profile = str(profile_id or "").strip()
    agent = str(agent_id or "").strip()
    env_name = str(token_env or "").strip()
    if not profile or not agent:
        raise ValueError("profile_id and agent_id are required")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
        raise ValueError("token_env must be a valid environment-variable name")
    document: dict[str, Any] = {"agents": {}}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"existing agents file is unreadable: {exc}") from exc
        if not isinstance(loaded, dict) or not isinstance(loaded.get("agents"), dict):
            raise ValueError("existing agents file must contain an agents object")
        document = loaded
    agents = document["agents"]
    if agent in agents and not replace_agent:
        raise ValueError(f"agent {agent!r} already exists; use --replace-agent to update it")
    item = {
        "token_env": env_name,
        "profile_id": profile,
        "agent_id": agent,
        "workspaces": _values(workspaces, "workspace_id"),
        "subject_ids": _values(subject_ids, "subject_id"),
        "audiences": sorted({str(value).strip() for value in audiences if str(value).strip()}),
        "permissions": list(DEFAULT_REMOTE_PERMISSIONS),
    }
    agents[agent] = item
    _atomic_json(path, document)
    return {
        "status": "ok",
        "agents_file": str(path),
        "agent": item,
        "token_env": env_name,
        "next_steps": [
            f"Set the same secret value in {env_name} on the server and remote Agent.",
            f"meta-memory serve --agents-file \"{path}\"",
            "meta-memory install-remote-agent --help",
        ],
    }


__all__ = ["DEFAULT_REMOTE_PERMISSIONS", "write_agent_binding"]
