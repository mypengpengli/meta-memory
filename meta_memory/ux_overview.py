"""One-screen readiness dashboard for interactive Meta Memory use."""
from __future__ import annotations

import json
from datetime import datetime, timezone
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
    """Normalize independent integration facts into an actionable summary.

    A launcher pass is intentionally not enough for ``ready``.  The overview
    becomes ready only after current integration files, a recorded launcher
    verification, and a real completed host lifecycle have all been observed.
    Missing newer fields default to the former ready semantics so third-party
    callers that still supply the old minimal status shape remain compatible.
    """

    def name(row: dict[str, Any]) -> str:
        return str(row.get("agent") or "unknown")

    def files_present(row: dict[str, Any]) -> bool:
        return bool(row.get("files_installed", row.get("installed")))

    def partially_present(row: dict[str, Any]) -> bool:
        return files_present(row) or str(row.get("installation_state") or "") == "partial"

    def contract_current(row: dict[str, Any]) -> bool:
        value = row.get("template_contract")
        if isinstance(value, dict):
            return bool(value.get("current", value.get("state") == "current"))
        if "template_contract_current" in row:
            return bool(row.get("template_contract_current"))
        return True

    def verification_current(row: dict[str, Any]) -> bool:
        # Old callers did not report verification.  Treat their known-good
        # launcher shape as compatible, while new status rows must be explicit.
        if "launcher_verified" not in row:
            return True
        return bool(row.get("launcher_verified"))

    def lifecycle(row: dict[str, Any]) -> str:
        return str(row.get("lifecycle_state") or "active")

    present = [row for row in rows if partially_present(row)]
    installed = [row for row in present if files_present(row)]
    names = [name(row) for row in present]
    if not present:
        return {"status": "not_installed", "installed": 0, "usable": 0, "agents": []}

    # A partial/manual copy without an installation registry cannot be
    # repaired by ``agent sync --all`` because there is no recorded source
    # path to regenerate. Report reinstall explicitly instead of offering a
    # command that is guaranteed to do nothing.
    unregistered = [
        row for row in present
        if isinstance(row.get("template_contract"), dict)
        and str(row["template_contract"].get("state") or "") == "not_installed"
    ]
    if unregistered:
        return {
            "status": "needs_install", "installed": len(installed), "usable": 0,
            "agents": names,
            "needs_install": [name(row) for row in unregistered],
            "reason": "partial_files_without_installation_registry",
        }

    broken = [
        row for row in present
        if not files_present(row)
        or row.get("skill") != "ok" or row.get("launcher") != "ok"
        or not bool(row.get("shared_config")) or not bool(row.get("shared_store"))
        or not contract_current(row)
    ]
    if broken:
        return {
            "status": "needs_sync", "installed": len(installed), "usable": 0,
            "agents": names,
            "needs_sync": [name(row) for row in broken],
            "reason": "integration_files_or_template_contract_not_current",
        }

    unverified = [row for row in installed if not verification_current(row)]
    if unverified:
        return {
            "status": "needs_verification", "installed": len(installed), "usable": 0,
            "agents": names, "needs_verification": [name(row) for row in unverified],
            "reason": "launcher_not_verified",
        }

    errored = [row for row in installed if lifecycle(row) == "error"]
    if errored:
        return {
            "status": "error", "installed": len(installed), "usable": 0,
            "agents": names, "errors": [name(row) for row in errored],
            "reason": "host_lifecycle_reported_error",
        }

    awaiting_activation = [row for row in installed if lifecycle(row) in {"never_seen", "before_only"}]
    if awaiting_activation:
        pending = [name(row) for row in awaiting_activation]
        return {
            "status": "needs_activation", "installed": len(installed), "usable": len(installed) - len(awaiting_activation),
            "agents": names, "awaiting_activation": pending,
            "manual_steps": [
                "Start one real conversation through the listed Agent host so it runs the before/after lifecycle.",
                "Then run meta-memory agent status --all --verbose and confirm lifecycle_state is active.",
            ],
            "reason": "awaiting_first_real_host_turn",
        }
    return {
        "status": "ready", "installed": len(installed), "usable": len(installed),
        "agents": names,
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


def _at_or_after(value: Any, baseline: Any) -> bool:
    if not value:
        return False
    if not baseline:
        return True
    try:
        left = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        right = datetime.fromisoformat(str(baseline).replace("Z", "+00:00"))
        return left >= right
    except (TypeError, ValueError):
        return str(value) >= str(baseline)


def overview(
    config: AppConfig,
    *,
    project_name: str = "auto",
    start: str | Path | None = None,
    agent_id: str = "",
    server: bool = False,
    agents_file: str | Path | None = None,
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
        # Shared-world tables are introduced by migration 024.  They are
        # profile-wide on purpose: a household activity or map may originate
        # in another workspace and still be useful to the current Agent.
        moment = datetime.now(timezone.utc).isoformat()
        activities = int(conn.execute(
            "SELECT COUNT(*) FROM shared_activities WHERE profile_id=? AND status='active' "
            "AND (valid_until IS NULL OR valid_until>?)",
            (config.profile_id, moment),
        ).fetchone()[0])
        temporal_states = int(conn.execute(
            "SELECT COUNT(*) FROM temporal_states WHERE profile_id=? AND status='active' "
            "AND valid_from<=? AND (valid_until IS NULL OR valid_until>?)",
            (config.profile_id, moment, moment),
        ).fetchone()[0])
        assets = int(conn.execute(
            "SELECT COUNT(*) FROM binary_assets WHERE profile_id=? AND status='active'",
            (config.profile_id,),
        ).fetchone()[0])
        maps = int(conn.execute(
            "SELECT COUNT(DISTINCT map_id) FROM spatial_maps WHERE profile_id=? AND status='active'",
            (config.profile_id,),
        ).fetchone()[0])
        observations = int(conn.execute(
            "SELECT COUNT(*) FROM spatial_observations WHERE profile_id=? AND status='active' "
            "AND (valid_until IS NULL OR valid_until>?)",
            (config.profile_id, moment),
        ).fetchone()[0])
        requested_agent = str(agent_id or "").strip()
        if requested_agent:
            latest = conn.execute(
                """
                SELECT agent_id,last_before_at,last_after_at,last_error_at,last_error_message
                FROM agent_runtime_state
                WHERE profile_id=? AND workspace_id=? AND agent_id=?
                LIMIT 1
                """,
                (config.profile_id, project.workspace_id, origin_agent_id(requested_agent)),
            ).fetchone()
        else:
            # A terminal overview has no launcher identity. Show the most
            # recently active real Agent instead of a permanently empty
            # generic-agent row.
            latest = conn.execute(
                """
                SELECT agent_id,last_before_at,last_after_at,last_error_at,last_error_message
                FROM agent_runtime_state
                WHERE profile_id=? AND workspace_id=?
                ORDER BY max(coalesce(last_before_at,''),coalesce(last_after_at,''),coalesce(last_error_at,'')) DESC
                LIMIT 1
                """,
                (config.profile_id, project.workspace_id),
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
    if server:
        agents = []
    else:
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
    hosted_readiness: dict[str, Any] | None = None
    if server:
        selected_agents = Path(agents_file).expanduser().resolve() if agents_file else None
        if selected_agents is None:
            hosted_readiness = {"status": "not_configured", "agents_file": "", "agents": 0}
        else:
            try:
                from .http_api import load_principals

                principals = load_principals(selected_agents)
                hosted_readiness = {
                    "status": "ready", "agents_file": str(selected_agents),
                    "agents": len(principals),
                }
            except (OSError, ValueError) as exc:
                hosted_readiness = {
                    "status": "error", "agents_file": str(selected_agents),
                    "agents": 0, "error": str(exc),
                }
    config_exists = Path(config.path).expanduser().is_file()
    last_error = (
        str(latest[4] or "")
        if latest and latest[4] and _at_or_after(latest[3], latest[2])
        else ""
    )
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
    if not server and agent_summary["status"] == "unknown":
        actions.append(_action("inspect_agent_integration", "meta-memory agent status --all --verbose", "Inspect why the Agent integration could not be read.", priority=55))
    elif not server and agent_summary["status"] == "error":
        actions.append(_action("inspect_agent_error", "meta-memory agent status --all --verbose", "Inspect the Agent lifecycle error before relying on automatic memory.", priority=55))
    if scheduler_summary["status"] == "unknown":
        actions.append(_action("inspect_schedule", "meta-memory schedule status", "Inspect why the local scheduler could not be read.", priority=55))

    if server and hosted_readiness and hosted_readiness["status"] == "not_configured":
        actions.append(_action(
            "create_server_agents_file", "meta-memory init-agents-file --help",
            "Create the server binding for at least one remote Agent.", priority=60,
        ))
    elif server and hosted_readiness and hosted_readiness["status"] == "error":
        actions.append(_action(
            "repair_server_agents_file", "meta-memory overview --server --agents-file <path>",
            "Set every token_env on the server and repair the Agent binding file.", priority=60,
        ))
    elif not config_exists:
        actions.append(_action("save_initial_setup", "meta-memory setup --agents codex", "Save configuration and connect your first Agent.", priority=60))
    elif not server and agent_summary["status"] == "not_installed":
        actions.append(_action("install_agent", "meta-memory install-agent codex", "Connect an Agent so conversations use memory automatically.", priority=70))
    elif not server and agent_summary["status"] == "needs_install":
        names = [str(name) for name in agent_summary.get("needs_install", [])]
        builtin = {"codex", "claude-code", "openclaw"}
        if names and all(name in builtin for name in names):
            command = "meta-memory install-agent " + " ".join(names)
            detail = "Reinstall the partially copied built-in Agent integration and recreate its registry."
        else:
            command = "meta-memory agent status --all --verbose"
            detail = "Inspect the partial custom integration, then rerun install-agent custom with its original Skill path."
        actions.append(_action("reinstall_agent", command, detail, priority=70))
    elif not server and agent_summary["status"] == "needs_sync":
        actions.append(_action("sync_agent", "meta-memory agent sync --all", "Refresh an installed Agent integration after an upgrade or move.", priority=70))
    elif not server and agent_summary["status"] == "needs_verification":
        names = [str(name) for name in agent_summary.get("needs_verification", [])]
        command = f"meta-memory agent verify {names[0]}" if len(names) == 1 else "meta-memory agent verify <agent-id>"
        actions.append(_action("verify_agent_launcher", command, "Verify each installed Agent launcher before its first real host turn.", priority=70))
    elif not server and agent_summary["status"] == "needs_activation":
        actions.append(_action("activate_agent_lifecycle", "meta-memory agent status --all --verbose", "Start one real conversation through the Agent host, then confirm its lifecycle is active.", priority=70))

    # First-time setup installs enabled schedules by default, so presenting a
    # second schedule command before setup would be redundant and confusing.
    if not server and config_exists and scheduler_summary["status"] == "not_installed":
        actions.append(_action("install_schedule", "meta-memory schedule install", "Install enabled background heartbeat and Dream tasks.", priority=80))

    actions.sort(key=lambda item: (int(item["priority"]), str(item["code"])))
    if server and hosted_readiness and hosted_readiness["status"] == "error":
        status = "degraded"
    elif server and (not config_exists or not hosted_readiness or hosted_readiness["status"] != "ready"):
        status = "needs_setup"
    elif server and (spool or unfinished or inbox or review or projections):
        status = "needs_action"
    elif server:
        status = "ready"
    elif last_error or agent_summary["status"] in {"unknown", "error"} or scheduler_summary["status"] == "unknown":
        status = "degraded"
    elif spool or unfinished or inbox or review or projections:
        status = "needs_action"
    elif agent_summary["status"] in {"needs_verification", "needs_activation"}:
        status = "needs_action"
    elif not config_exists or agent_summary["status"] != "ready" or scheduler_summary["status"] == "not_installed":
        status = "needs_setup"
    else:
        status = "ready"

    issue = actions[0]["code"] if actions else None
    return {
        "status": status,
        "mode": "hosted_server" if server else "local_agent",
        "project": {"id": project.project_id, "workspace_id": project.workspace_id, "root": str(project.root)},
        "counts": {
            "active_claims": claims, "resources": resources, "inbox": inbox, "unfinished_turns": unfinished,
            "pending_review_jobs": review, "pending_projections": projections, "pending_spool": spool,
            "shared_activities": activities, "current_states": temporal_states,
            "binary_assets": assets, "spatial_maps": maps, "spatial_observations": observations,
        },
        "agent": {
            "id": str(latest[0]) if latest else origin_agent_id(agent_id),
            "last_before": latest[1] if latest else None,
            "last_after": latest[2] if latest else None,
            "last_error": last_error or None,
        },
        "agents": agents,
        "dream": dream,
        "scheduler": scheduler,
        "readiness": {
            "configuration": {"status": "ready" if config_exists else "not_saved", "path": str(config.path)},
            "agent": agent_summary,
            "scheduler": scheduler_summary,
            **({"hosted_server": hosted_readiness} if hosted_readiness is not None else {}),
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
    hosted_state = readiness.get("hosted_server") if isinstance(readiness.get("hosted_server"), dict) else None
    agent_names = ", ".join(str(name) for name in agent_state.get("agents", [])) or "none"
    expected_schedule = ", ".join(str(name) for name in scheduler_state.get("expected", [])) or "disabled"
    lines = [
        f"Meta Memory · {str(overview_value.get('status') or 'unknown').upper()}",
        f"Project: {project.get('id') or 'unknown'}",
        "",
        "Readiness",
        f"  configuration: {config_state.get('status') or 'unknown'}",
    ]
    if hosted_state is not None:
        lines.append(
            f"  hosted server: {hosted_state.get('status') or 'unknown'} "
            f"({hosted_state.get('agents', 0)} Agent bindings)"
        )
        if hosted_state.get("agents_file"):
            lines.append(f"  agents file: {hosted_state.get('agents_file')}")
    else:
        lines.extend([
            f"  agent: {agent_state.get('status') or 'unknown'} ({agent_names})",
            f"  schedule: {scheduler_state.get('status') or 'unknown'} ({expected_schedule})",
        ])
    lines.extend([
        "",
        "Memory",
        f"  active claims: {counts.get('active_claims', 0)}  imported resources: {counts.get('resources', 0)}",
        f"  shared activity: {counts.get('shared_activities', 0)}  current states: {counts.get('current_states', 0)}",
        f"  spatial: {counts.get('spatial_maps', 0)} maps, {counts.get('spatial_observations', 0)} observations, {counts.get('binary_assets', 0)} assets",
        f"  pending review: {counts.get('inbox', 0)}  unfinished turns: {counts.get('unfinished_turns', 0)}",
        f"  queued work: review jobs {counts.get('pending_review_jobs', 0)}, projections {counts.get('pending_projections', 0)}, spool {counts.get('pending_spool', 0)}",
        "",
        "Recent activity",
        f"  agent: {agent.get('id') or 'generic-agent'}",
        f"  last context load: {_short_time(agent.get('last_before'))}",
        f"  last saved reply: {_short_time(agent.get('last_after'))}",
    ])
    manual_steps = agent_state.get("manual_steps") if isinstance(agent_state.get("manual_steps"), list) else []
    if manual_steps:
        lines.extend(["  Agent activation:"])
        lines.extend(f"    - {step}" for step in manual_steps)
    actions = overview_value.get("actions") if isinstance(overview_value.get("actions"), list) else []
    if actions:
        lines.extend(["", "Recommended actions"])
        for index, action in enumerate(actions, start=1):
            if not isinstance(action, dict):
                continue
            lines.append(f"  {index}. {action.get('summary') or action.get('code')}")
            lines.append(f"     {action.get('command')}")
    elif hosted_state is not None:
        lines.extend([
            "",
            "Hosted configuration is ready. Confirm the live API with /readyz, keep exactly one maintenance worker/schedule running, then verify each remote Agent with:",
            "  <remote-launcher> status",
        ])
    else:
        lines.extend(["", "Ready for normal use. Talk to a connected Agent, or save a fact with:", "  meta-memory remember --content \"...\""])
    return "\n".join(lines)
