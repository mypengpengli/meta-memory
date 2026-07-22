"""Render a non-secret remote Meta Memory Skill and its launchers.

This module is intentionally independent from the local Agent installer and
public CLI so applications can embed it without changing local integration
semantics.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import sys
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any

from .agent_specs import normalize_agent_id
from .remote_client import REMOTE_CLIENT_CONTRACT_VERSION, _atomic_json, _validate_url


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _render(name: str, **values: str) -> str:
    text = files("meta_memory").joinpath("templates", name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{ " + key + " }}", value)
    unresolved = re.findall(r"{{\s*[^}]+\s*}}", text)
    if unresolved:
        raise RuntimeError(f"unresolved remote Skill template fields: {', '.join(unresolved)}")
    return text


def _ps_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _cmd_quote(path: Path) -> str:
    # Percent expansion happens even inside cmd.exe double quotes.
    return '"' + str(path).replace("%", "%%") + '"'


def _launcher_help(
    posix: Path,
    windows: Path,
    *,
    executable: Path,
    config_path: Path,
    agent: str,
) -> str:
    direct = [
        str(executable), "-m", "meta_memory.remote_client", "--config", str(config_path),
        "--agent-id", agent,
    ]
    return "\n".join(
        [
            "Choose the invocation that matches the host's process API or shell:",
            "",
            f"- Direct process API: argv `{json.dumps(direct, ensure_ascii=False)}` followed by command arguments.",
            f"- PowerShell: `& {_ps_quote(windows)}`",
            f"- Windows cmd.exe: `call {_cmd_quote(windows)}`",
            f"- POSIX shell or Git Bash: `{shlex.quote(str(posix))}`",
        ]
    )


def install_remote_agent(
    agent_id: str,
    skill_dir: str | Path,
    server_url: str,
    workspace_id: str,
    subject_id: str,
    *,
    audience_id: str = "",
    channel_id: str = "",
    token_env: str = "META_MEMORY_TOKEN",
    outbox_dir: str | Path | None = None,
    python_executable: str | Path | None = None,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Install a remote-only integration below an arbitrary host Skill root.

    ``token_env`` is an environment-variable *name*.  This API never accepts a
    bearer-token value, which prevents accidental persistence in generated
    configuration, launchers, or Skill instructions.
    """

    agent = normalize_agent_id(agent_id, allow_builtin=True)
    root_text = str(skill_dir or "").strip()
    if not root_text:
        raise ValueError("skill_dir is required")
    workspace = str(workspace_id or "").strip()
    subject = str(subject_id or "").strip()
    if not workspace or not subject:
        raise ValueError("workspace_id and subject_id are required stable identities")
    env_name = str(token_env or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
        raise ValueError("token_env must be a valid environment variable name")
    url = _validate_url(server_url)
    target = Path(root_text).expanduser().resolve() / "meta-memory-remote"
    target.mkdir(parents=True, exist_ok=True)
    installed_at = _now()
    if outbox_dir is not None:
        outbox = Path(outbox_dir).expanduser().resolve()
    else:
        scope = hashlib.sha256(f"{url}\0{workspace}\0{subject}".encode("utf-8")).hexdigest()[:16]
        outbox = (Path.home() / ".meta-memory" / "remote" / agent / scope / "outbox").resolve()
    config_path = target / "remote-config.json"
    config = {
        "version": 1,
        "client_contract_version": REMOTE_CLIENT_CONTRACT_VERSION,
        "url": url,
        "token_env": env_name,
        "agent_id": agent,
        "workspace_id": workspace,
        "subject_id": subject,
        "audience_id": str(audience_id or "").strip(),
        "channel_id": str(channel_id or "").strip(),
        "outbox_dir": str(outbox),
        "timeout_seconds": max(1.0, min(120.0, float(timeout_seconds))),
        "installed_at": installed_at,
    }
    _atomic_json(config_path, config)

    executable = Path(python_executable or sys.executable).expanduser().resolve()
    posix = target / "meta-memory-remote"
    windows = target / "meta-memory-remote.cmd"
    with posix.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(
            "#!/bin/sh\nexec " + shlex.quote(str(executable)) + " -m meta_memory.remote_client --config "
            + shlex.quote(str(config_path)) + " --agent-id " + shlex.quote(agent) + ' "$@"\n'
        )
    try:
        posix.chmod(posix.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass
    with windows.open("w", encoding="utf-8", newline="") as stream:
        stream.write(
            "@echo off\r\n" + _cmd_quote(executable) + " -m meta_memory.remote_client --config "
            + _cmd_quote(config_path) + " --agent-id " + agent + " %*\r\n"
        )
    skill = target / "SKILL.md"
    skill.write_text(
        _render(
            "remote-skill.md.template",
            agent_id=agent,
            token_env=env_name,
            installed_at=installed_at,
            launcher_shell_help=_launcher_help(
                posix, windows, executable=executable, config_path=config_path, agent=agent
            ),
        ),
        encoding="utf-8",
    )
    return {
        "status": "needs_action",
        "installation_status": "ok",
        "agent_id": agent,
        "skill": str(skill),
        "config": str(config_path),
        "launcher": str(windows if os.name == "nt" else posix),
        "launchers": {"posix": str(posix), "windows": str(windows)},
        "direct_argv": [
            str(executable), "-m", "meta_memory.remote_client", "--config", str(config_path),
            "--agent-id", agent,
        ],
        "server_url": url,
        "workspace_id": workspace,
        "subject_id": subject,
        "audience_id": str(audience_id or "").strip(),
        "channel_id": str(channel_id or "").strip(),
        "token_env": env_name,
        "token_persisted": False,
        "installed_at": installed_at,
        "activation_status": "awaiting_first_remote_turn",
        "next_action": f"Set {env_name}, restart the Agent, complete one ordinary Turn, then run the generated launcher status command.",
    }
