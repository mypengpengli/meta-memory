"""One-screen operational overview and compact terminal rendering."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import AppConfig
from .legacy import bootstrap
from .project_detection import resolve_project


def _ready(config: AppConfig):
    bootstrap()
    from _common import ensure_store_ready, open_db

    root = Path(config.store)
    ensure_store_ready(root)
    return root, open_db(root)


def overview(
    config: AppConfig,
    *,
    project_name: str = "auto",
    start: str | Path | None = None,
    agent_id: str = "",
) -> dict[str, Any]:
    """Return a stable, user-oriented readiness summary.

    The result deliberately avoids memory bodies.  It is safe to inspect in a
    terminal and is useful as a first command when a user is unsure whether a
    previous turn was durably captured.
    """
    from .runtime import origin_agent_id

    project = resolve_project(config, project_name, start)
    _, conn = _ready(config)
    try:
        scope = (config.profile_id, project.workspace_id, config.subject_id)
        inbox = int(conn.execute(
            "SELECT COUNT(*) FROM write_proposals WHERE profile_id=? AND workspace_id=? AND subject_id=? AND status IN ('pending','needs_clarification')",
            scope,
        ).fetchone()[0])
        unfinished = int(conn.execute(
            "SELECT COUNT(*) FROM turns WHERE profile_id=? AND workspace_id=? AND subject_id=? AND status IN ('started','abandoned')",
            scope,
        ).fetchone()[0])
        review = int(conn.execute(
            "SELECT COUNT(*) FROM review_jobs WHERE profile_id=? AND workspace_id=? AND status IN ('pending','running','failed')",
            (config.profile_id, project.workspace_id),
        ).fetchone()[0])
        projections = int(conn.execute("SELECT COUNT(*) FROM projection_outbox WHERE status IN ('pending','running','failed')").fetchone()[0])
        claims = int(conn.execute(
            "SELECT COUNT(*) FROM claims WHERE profile_id=? AND workspace_id=? AND subject_id=? AND status='active'",
            scope,
        ).fetchone()[0])
        resources = int(conn.execute(
            "SELECT COUNT(*) FROM resource_imports WHERE profile_id=? AND workspace_id=? AND subject_id=?",
            scope,
        ).fetchone()[0])
        latest = conn.execute(
            "SELECT last_before_at,last_after_at,last_error_message FROM agent_runtime_state WHERE profile_id=? AND workspace_id=? AND agent_id=?",
            (config.profile_id, project.workspace_id, origin_agent_id(agent_id)),
        ).fetchone()
    finally:
        conn.close()
    from .spool import pending_dir

    spool = len(list(pending_dir(config).glob("*.json"))) if pending_dir(config).is_dir() else 0
    try:
        from .dream_heartbeat import dream_status

        dream = dream_status(config)
    except (OSError, RuntimeError):
        dream = {"status": "unknown"}
    try:
        from .scheduler import schedule_status

        scheduler = schedule_status(config)
    except (OSError, RuntimeError) as exc:
        scheduler = {"status": "warning", "error": str(exc)}
    try:
        from .agent_status import agent_status

        agents = agent_status(
            config, agent_id=origin_agent_id(agent_id), installed_default=True,
            project_name=project_name, start=start,
        ).get("agents", [])
    except (OSError, RuntimeError):
        agents = []
    issue = ""
    next_action = ""
    status = "ready"
    if spool:
        status, issue, next_action = "needs_action", "completion_spool_pending", "meta-memory recovery replay"
    elif unfinished:
        status, issue, next_action = "needs_action", "unfinished_turns", "meta-memory turn list --unfinished"
    elif inbox:
        status, issue, next_action = "needs_action", "inbox_pending", "meta-memory inbox list"
    elif review or projections:
        status, issue, next_action = "needs_action", "maintenance_pending", "meta-memory dream heartbeat"
    elif not agents:
        status, issue, next_action = "needs_action", "no_agent_installed", "meta-memory install-agent codex"
    last_error = str(latest[2] or "") if latest else ""
    if last_error:
        status, issue, next_action = "degraded", "agent_runtime_error", "meta-memory agent status --all --verbose"
    return {
        "status": status,
        "project": {"id": project.project_id, "workspace_id": project.workspace_id, "root": str(project.root)},
        "counts": {
            "active_claims": claims, "resources": resources, "inbox": inbox, "unfinished_turns": unfinished,
            "pending_review_jobs": review, "pending_projections": projections, "pending_spool": spool,
        },
        "agent": {
            "id": origin_agent_id(agent_id), "last_before": latest[0] if latest else None,
            "last_after": latest[1] if latest else None, "last_error": last_error or None,
        },
        "agents": agents, "dream": dream, "scheduler": scheduler,
        "issue": issue or None, "next_action": next_action or None,
    }


def human_text(value: Any) -> str:
    """Render only the high-value operational fields for an interactive TTY."""
    overview_value: dict[str, Any] | None = None
    if isinstance(value, dict):
        if {"project", "counts", "next_action"}.issubset(value):
            overview_value = value
        elif isinstance(value.get("overview"), dict):
            overview_value = value["overview"]
    if overview_value is None:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    project = overview_value.get("project") if isinstance(overview_value.get("project"), dict) else {}
    counts = overview_value.get("counts") if isinstance(overview_value.get("counts"), dict) else {}
    agent = overview_value.get("agent") if isinstance(overview_value.get("agent"), dict) else {}
    lines = [
        f"Meta Memory: {str(overview_value.get('status') or 'unknown').upper()}",
        f"Project: {project.get('id') or 'unknown'}",
        "",
        "Memory:",
        f"  active claims: {counts.get('active_claims', 0)}  resources: {counts.get('resources', 0)}",
        f"  inbox: {counts.get('inbox', 0)}  unfinished turns: {counts.get('unfinished_turns', 0)}",
        f"  queued work: reviews {counts.get('pending_review_jobs', 0)}, projections {counts.get('pending_projections', 0)}, spool {counts.get('pending_spool', 0)}",
        "",
        f"Agent: {agent.get('id') or 'generic-agent'}  last before: {agent.get('last_before') or '-'}  last after: {agent.get('last_after') or '-'}",
    ]
    if overview_value.get("issue"):
        lines.extend(["", f"Issue: {overview_value['issue']}"])
    if overview_value.get("next_action"):
        lines.append(f"Next action: {overview_value['next_action']}")
    return "\n".join(lines)
