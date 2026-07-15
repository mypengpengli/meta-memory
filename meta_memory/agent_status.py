"""Safe local visibility into Agent integration and runtime health."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import AppConfig
from .project_detection import resolve_project
from .runtime_audit import project_identity_warnings, repository_fingerprint


def _registry_path(config: AppConfig, agent_id: str) -> Path:
    return Path(config.path).expanduser().resolve().parent / "agents" / f"{agent_id}.json"


def _registry(config: AppConfig, agent_id: str) -> dict[str, object]:
    path = _registry_path(config, agent_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError, json.JSONDecodeError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _known_agents(config: AppConfig, *, workspace_id: str | None = None) -> list[str]:
    from _common import open_db

    names = {path.stem for path in (_registry_path(config, "_").parent.glob("*.json"))} if _registry_path(config, "_").parent.is_dir() else set()
    conn = open_db(Path(config.store))
    try:
        sql = "SELECT DISTINCT agent_id FROM agent_runtime_state WHERE profile_id=?" + (" AND workspace_id=?" if workspace_id else "")
        params = (config.profile_id, workspace_id) if workspace_id else (config.profile_id,)
        names.update(str(row[0]) for row in conn.execute(sql, params) if str(row[0] or ""))
    finally:
        conn.close()
    return sorted(names)


def _state(config: AppConfig, *, agent_id: str, workspace_id: str) -> dict[str, object]:
    from _common import open_db

    conn = open_db(Path(config.store))
    try:
        row = conn.execute(
            """
            SELECT project_id,project_root,repository_fingerprint,last_before_at,last_after_at,last_write_at,last_retrieval_at,
                   last_turn_uid,last_session_id,last_retrieval_count,last_retrieval_duration_ms,total_before,total_after,
                   total_degraded,last_error_at,last_error_code,last_error_message
            FROM agent_runtime_state WHERE profile_id=? AND agent_id=? AND workspace_id=?
            """,
            (config.profile_id, agent_id, workspace_id),
        ).fetchone()
        unfinished = int(conn.execute("SELECT COUNT(*) FROM turns WHERE profile_id=? AND origin_agent_id=? AND workspace_id=? AND status='started'", (config.profile_id, agent_id, workspace_id)).fetchone()[0])
        runtime = conn.execute("SELECT claim_generation,hot_generation,hot_dirty,dream_dirty FROM workspace_runtime_state WHERE profile_id=? AND workspace_id=? AND subject_id=? AND agent_id=''", (config.profile_id, workspace_id, config.subject_id)).fetchone()
        session_hot = conn.execute(
            """
            SELECT hot_generation FROM sessions
            WHERE profile_id=? AND workspace_id=? AND subject_id=?
              AND external_session_id=? AND origin_agent_id=?
            ORDER BY last_active_at DESC LIMIT 1
            """,
            (config.profile_id, workspace_id, config.subject_id, str(row[8] or "") if row else "", agent_id),
        ).fetchone()
        pending_review = int(conn.execute("SELECT COUNT(*) FROM review_jobs WHERE profile_id=? AND workspace_id=? AND status IN ('pending','running','failed')", (config.profile_id, workspace_id)).fetchone()[0])
        # The durable outbox predates scoped runtime rows and deliberately has
        # no profile/workspace columns.  Expose only its aggregate queue depth
        # instead of pretending it can be filtered to the current agent.
        pending_projection = int(conn.execute("SELECT COUNT(*) FROM projection_outbox WHERE status IN ('pending','running','failed')").fetchone()[0])
    finally:
        conn.close()
    keys = ["project_id", "project_root", "repository_fingerprint", "last_before", "last_after", "last_write", "last_retrieval", "last_turn_uid", "last_session_id", "last_retrieval_count", "last_retrieval_duration_ms", "total_before", "total_after", "total_degraded", "last_error_at", "last_error_code", "last_error_message"]
    data = dict(zip(keys, row)) if row else {}
    data.update({"unfinished_turns": unfinished, "claim_generation": int(runtime[0] or 0) if runtime else 0, "hot_generation": int(runtime[1] or 0) if runtime else 0, "session_hot_generation": int(session_hot[0] or 0) if session_hot else 0, "hot_dirty": bool(runtime[2]) if runtime else False, "dream_dirty": bool(runtime[3]) if runtime else False, "pending_review_jobs": pending_review, "pending_projections": pending_projection})
    return data


def _one_status(config: AppConfig, *, agent_id: str, project, verbose: bool) -> dict[str, object]:
    from .agent_specs import detect_agent
    from .spool import pending_dir

    registry = _registry(config, agent_id)
    state = _state(config, agent_id=agent_id, workspace_id=project.workspace_id)
    try:
        identity_warnings = project_identity_warnings(config, agent_id=agent_id, project=project)
    except Exception:
        identity_warnings = []
    launcher = Path(str(registry.get("launcher") or (Path(config.path).expanduser().resolve().parent / "bin" / f"meta-memory-{agent_id}{'.cmd' if os.name == 'nt' else ''}")))
    skill = Path(str(registry.get("skill") or "")) if registry.get("skill") else None
    configured = str(Path(config.path).expanduser().resolve())
    store = str(Path(config.store).expanduser().resolve())
    try:
        detected = detect_agent(agent_id) if agent_id in {"codex", "claude-code", "openclaw"} else bool(registry)
    except ValueError:
        detected = bool(registry)
    result: dict[str, object] = {
        "status": "ok",
        "agent": agent_id,
        "detected": detected,
        "installed": bool(registry) or launcher.is_file(),
        "skill": "ok" if skill and skill.is_file() else "not_found",
        "launcher": "ok" if launcher.is_file() else "not_found",
        "shared_config": str(registry.get("config") or configured) == configured,
        "shared_store": str(registry.get("store") or store) == store,
        "project": project.project_id,
        "last_before": state.get("last_before"),
        "last_after": state.get("last_after"),
        "last_retrieval_count": int(state.get("last_retrieval_count") or 0),
        "last_retrieval_ms": int(state.get("last_retrieval_duration_ms") or 0),
        "unfinished_turns": int(state["unfinished_turns"]),
        "pending_spool": len(list(pending_dir(config).glob("*.json"))) if pending_dir(config).is_dir() else 0,
        "last_error": state.get("last_error_message"),
    }
    if verbose:
        result["details"] = {
            "agent_id": agent_id, "integration_type": registry.get("integration_type", "unknown"),
            "launcher_path": str(launcher), "skill_path": str(skill) if skill else None,
            "host_instruction_file": registry.get("host_instruction"), "config_path": configured, "store_path": store,
            "project_id": project.project_id, "workspace_id": project.workspace_id, "project_root": str(project.root),
            "repository_fingerprint": repository_fingerprint(project), "session_id": state.get("last_session_id"),
            "last_turn_uid": state.get("last_turn_uid"), "claim_generation": state["claim_generation"],
            "hot_generation": state["hot_generation"], "session_hot_generation": state["session_hot_generation"],
            "dirty_scope": bool(state["hot_dirty"] or state["dream_dirty"]), "project_identity_warnings": identity_warnings,
            "pending_review_jobs": state["pending_review_jobs"], "pending_projections": state["pending_projections"],
            "total_before": int(state.get("total_before") or 0), "total_after": int(state.get("total_after") or 0),
            "total_degraded": int(state.get("total_degraded") or 0), "last_error_code": state.get("last_error_code"),
        }
    return result


def agent_status(config: AppConfig, *, agent_id: str, all_agents: bool = False, project_name: str = "auto", start: str | Path | None = None, verbose: bool = False) -> dict[str, object]:
    project = resolve_project(config, project_name, start)
    agents = _known_agents(config, workspace_id=project.workspace_id) if all_agents else [agent_id]
    rows = [_one_status(config, agent_id=name, project=project, verbose=verbose) for name in agents]
    return {"status": "ok", "project": project.project_id, "agents": rows if all_agents else [], **({} if all_agents else rows[0])}


def verify_agent(config: AppConfig, *, agent_id: str, project_name: str = "auto", start: str | Path | None = None) -> dict[str, object]:
    from _common import ensure_store_ready
    from .skill_installer import _verify_launcher

    project = resolve_project(config, project_name, start)
    value = _one_status(config, agent_id=agent_id, project=project, verbose=True)
    registry = _registry(config, agent_id)
    launcher = Path(str(registry.get("launcher") or ""))
    verified, cli_status, detail = _verify_launcher(launcher) if launcher.is_file() else (False, "not_found", "launcher is missing")
    store = ensure_store_ready(Path(config.store))
    return {
        "status": "ok" if verified else "error", "agent": agent_id, "launcher_verified": verified,
        "launcher_status": cli_status, "detail": detail, "store": store, "project": project.project_id,
        "runtime": value,
    }
