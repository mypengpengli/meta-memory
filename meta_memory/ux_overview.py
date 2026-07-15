"""One-screen readiness dashboard for interactive Meta Memory use."""
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


def _agent_readiness(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Normalize the Agent status API into a small, actionable summary."""
    installed = [row for row in rows if bool(row.get("installed"))]
    usable = [
        row for row in installed
        if row.get("skill") == "ok" and row.get("launcher") == "ok"
        and bool(row.get("shared_config")) and bool(row.get("shared_store"))
    ]
    broken = [row for row in installed if row not in usable]
    if not installed:
        return {"status": "not_installed", "installed": 0, "usable": 0, "agents": []}
    if broken:
        return {
            "status": "needs_sync", "installed": len(installed), "usable": len(usable),
            "agents": [str(row.get("agent") or "unknown") for row in installed],
        }
    return {
        "status": "ready", "installed": len(installed), "usable": len(usable),
        "agents": [str(row.get("agent") or "unknown") for row in usable],
    }


def _scheduler_readiness(scheduler: dict[str, Any]) -> dict[str, Any]:
    """Treat enabled-but-uninstalled schedules as setup work, not "ready"."""
    if str(scheduler.get("status") or "").casefold() not in {"", "ok"}:
        return {
            "status": "unknown", "expected": [], "installed": [],
            "error": str(scheduler.get("error") or "could not inspect the local scheduler"),
        }
    expected = [str(item) for item in scheduler.get("expected", [])]
    if not expected:
        return {"status": "disabled", "expected": [], "installed": []}
    rows = scheduler.get("tasks")
    if isinstance(rows, list):
        installed = {str(row.get("action")) for row in rows if isinstance(row, dict) and bool(row.get("installed"))}
    elif bool(scheduler.get("managed_block")):
        # On Linux one managed crontab block contains every enabled action.
        installed = set(expected)
    else:
        installed = set()
    missing = [action for action in expected if action not in installed]
    launcher_exists = bool(scheduler.get("launcher_exists"))
    if missing or not launcher_exists:
        return {
            "status": "not_installed", "expected": expected, "installed": sorted(installed),
            "missing": missing or expected,
        }
    return {"status": "ready", "expected": expected, "installed": sorted(installed), "missing": []}


def _action(code: str, command: str, summary: str, *, priority: int) -> dict[str, Any]:
    return {"code": code, "command": command, "summary": summary, "priority": priority}


def _short_time(value: Any) -> str:
    if not value:
        return "never"
    text = str(value).replace("T", " ")
    return text.replace("+00:00", " UTC")


def overview(
    config: AppConfig,
    *,
    project_name: str = "auto",
    start: str | Path | None = None,
    agent_id: str = "",
) -> dict[str, Any]:
    """Return a stable dashboard with explicit readiness and next actions.

    The payload intentionally includes counts and operational state, but never
    memory bodies.  It remains safe for terminals, host integrations, and the
    ``status`` command while making an incomplete first-time setup obvious.
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
        scheduler = {"status": "warning", "error": str(exc), "expected": []}
    agent_error = ""
    try:
        from .agent_status import agent_status

        agents = agent_status(
            config, agent_id=origin_agent_id(agent_id), installed_default=True,
            project_name=project_name, start=start,
        ).get("agents", [])
    except (OSError, RuntimeError) as exc:
        agents = []
        agent_error = str(exc)

    agent_summary = _agent_readiness([row for row in agents if isinstance(row, dict)])
    if agent_error:
        agent_summary = {"status": "unknown", "installed": 0, "usable": 0, "agents": [], "error": agent_error}
    scheduler_summary = _scheduler_readiness(scheduler)
    config_exists = Path(config.path).expanduser().is_file()
    last_error = str(latest[2] or "") if latest else ""
    actions: list[dict[str, Any]] = []

    if spool:
        actions.append(_action("replay_completion_spool", "meta-memory recovery replay", "Replay deferred turn completions before continuing.", priority=10))
    if unfinished:
        actions.append(_action("review_unfinished_turns", "meta-memory turn list --unfinished", "Review or resume unfinished turns.", priority=20))
    if last_error:
        actions.append(_action("inspect_agent_error", "meta-memory agent status --all --verbose", "Inspect the latest Agent runtime error.", priority=30))
    if inbox:
        actions.append(_action("review_memory_proposals", "meta-memory inbox list", "Review pending memory proposals.", priority=40))
    if review or projections:
        actions.append(_action("run_heartbeat", "meta-memory dream heartbeat", "Process queued consolidation work now.", priority=50))
    if agent_summary["status"] == "unknown":
        actions.append(_action("inspect_agent_integration", "meta-memory agent status --all --verbose", "Inspect why the Agent integration could not be read.", priority=55))
    if scheduler_summary["status"] == "unknown":
        actions.append(_action("inspect_schedule", "meta-memory schedule status", "Inspect why the local scheduler could not be read.", priority=55))

    if not config_exists:
        actions.append(_action("save_initial_setup", "meta-memory setup --agents codex", "Save configuration and connect your first Agent.", priority=60))
    elif agent_summary["status"] == "not_installed":
        actions.append(_action("install_agent", "meta-memory install-agent codex", "Connect an Agent so conversations use memory automatically.", priority=70))
    elif agent_summary["status"] == "needs_sync":
        actions.append(_action("sync_agent", "meta-memory agent sync --all", "Refresh an installed Agent integration after an upgrade or move.", priority=70))

    # First-time setup installs enabled schedules by default, so presenting a
    # second schedule command before setup would be redundant and confusing.
    if config_exists and scheduler_summary["status"] == "not_installed":
        actions.append(_action("install_schedule", "meta-memory schedule install", "Install enabled background heartbeat and Dream tasks.", priority=80))

    actions.sort(key=lambda item: (int(item["priority"]), str(item["code"])))
    if last_error or agent_summary["status"] == "unknown" or scheduler_summary["status"] == "unknown":
        status = "degraded"
    elif spool or unfinished or inbox or review or projections:
        status = "needs_action"
    elif not config_exists or agent_summary["status"] != "ready" or scheduler_summary["status"] == "not_installed":
        status = "needs_setup"
    else:
        status = "ready"

    issue = actions[0]["code"] if actions else None
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
        "agents": agents,
        "dream": dream,
        "scheduler": scheduler,
        "readiness": {
            "configuration": {"status": "ready" if config_exists else "not_saved", "path": str(config.path)},
            "agent": agent_summary,
            "scheduler": scheduler_summary,
        },
        "actions": actions,
        "issue": issue,
        "next_action": actions[0]["command"] if actions else None,
    }


def human_text(value: Any) -> str:
    """Render a scannable dashboard while retaining JSON for automation."""
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
    readiness = overview_value.get("readiness") if isinstance(overview_value.get("readiness"), dict) else {}
    config_state = readiness.get("configuration") if isinstance(readiness.get("configuration"), dict) else {}
    agent_state = readiness.get("agent") if isinstance(readiness.get("agent"), dict) else {}
    scheduler_state = readiness.get("scheduler") if isinstance(readiness.get("scheduler"), dict) else {}
    agent_names = ", ".join(str(name) for name in agent_state.get("agents", [])) or "none"
    expected_schedule = ", ".join(str(name) for name in scheduler_state.get("expected", [])) or "disabled"
    lines = [
        f"Meta Memory · {str(overview_value.get('status') or 'unknown').upper()}",
        f"Project: {project.get('id') or 'unknown'}",
        "",
        "Readiness",
        f"  configuration: {config_state.get('status') or 'unknown'}",
        f"  agent: {agent_state.get('status') or 'unknown'} ({agent_names})",
        f"  schedule: {scheduler_state.get('status') or 'unknown'} ({expected_schedule})",
        "",
        "Memory",
        f"  active claims: {counts.get('active_claims', 0)}  imported resources: {counts.get('resources', 0)}",
        f"  pending review: {counts.get('inbox', 0)}  unfinished turns: {counts.get('unfinished_turns', 0)}",
        f"  queued work: review jobs {counts.get('pending_review_jobs', 0)}, projections {counts.get('pending_projections', 0)}, spool {counts.get('pending_spool', 0)}",
        "",
        "Recent activity",
        f"  agent: {agent.get('id') or 'generic-agent'}",
        f"  last context load: {_short_time(agent.get('last_before'))}",
        f"  last saved reply: {_short_time(agent.get('last_after'))}",
    ]
    actions = overview_value.get("actions") if isinstance(overview_value.get("actions"), list) else []
    if actions:
        lines.extend(["", "Recommended actions"])
        for index, action in enumerate(actions, start=1):
            if not isinstance(action, dict):
                continue
            lines.append(f"  {index}. {action.get('summary') or action.get('code')}")
            lines.append(f"     {action.get('command')}")
    else:
        lines.extend(["", "Ready for normal use. Talk to a connected Agent, or save a fact with:", "  meta-memory remember --content \"...\""])
    return "\n".join(lines)
