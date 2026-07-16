"""Safe local visibility into Agent integration and runtime health.

The status payload deliberately reports several independent facts.  In
particular, a generated launcher can be healthy while the host has never
loaded the Skill's lifecycle instructions.  Keeping those facts separate
prevents a green ``verify`` result from being mistaken for end-to-end host
activation.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import AppConfig
from .legacy import bootstrap
from .project_detection import resolve_project
from .runtime_audit import project_identity_warnings, repository_fingerprint


LAUNCHER_VERIFICATION_SCOPE = (
    "Runs the generated launcher’s status command. It validates the local "
    "launcher, configuration, and store path; it does not prove the host "
    "loaded its lifecycle instructions."
)
LIFECYCLE_OBSERVATION_SCOPE = (
    "Observed from Meta Memory runtime audit records after a real host-facing "
    "before/after invocation that occurred after this integration was last "
    "installed or synced. It confirms recorded lifecycle activity, not that "
    "the host’s hook is currently loaded."
)


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
    # The legacy database helper is a script module.  Bootstrap before import
    # so ``agent status`` works from both source and installed packages.
    bootstrap()
    from _common import open_db

    registry_dir = _registry_path(config, "_").parent
    names = {path.stem for path in registry_dir.glob("*.json")} if registry_dir.is_dir() else set()
    conn = open_db(Path(config.store))
    try:
        sql = "SELECT DISTINCT agent_id FROM agent_runtime_state WHERE profile_id=?" + (" AND workspace_id=?" if workspace_id else "")
        params = (config.profile_id, workspace_id) if workspace_id else (config.profile_id,)
        names.update(str(row[0]) for row in conn.execute(sql, params) if str(row[0] or ""))
    finally:
        conn.close()
    return sorted(names)


def _state(config: AppConfig, *, agent_id: str, workspace_id: str) -> dict[str, object]:
    bootstrap()
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


def _builtin_paths(agent_id: str) -> tuple[Path | None, Path | None]:
    """Return conventional files for a built-in host when no registry exists."""

    from .agent_specs import get_agent_spec

    try:
        spec = get_agent_spec(agent_id)
    except ValueError:
        return None, None
    return spec.skill_dir / "SKILL.md", spec.host_instruction_file


def _host_instruction_state(path: Path | None) -> str:
    if path is None:
        return "not_required"
    if not path.is_file():
        return "missing"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "unreadable"
    if "<!-- meta-memory:begin -->" in text and "<!-- meta-memory:end -->" in text:
        return "managed_block_present"
    return "managed_block_missing"


def _at_or_after(value: object, baseline: object | None) -> bool:
    """Compare ISO timestamps defensively, with a lexical fallback."""

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


def _lifecycle_state(
    state: dict[str, object],
    *,
    installed_at: object | None = None,
    require_install_baseline: bool = False,
) -> str:
    """Classify current-integration audit evidence without inferring a hook.

    Synchronizing regenerates the host contract and registry.  Therefore an
    old successful turn cannot establish that the newly generated contract was
    ever loaded; registered integrations only count events at or after their
    ``installed_at`` baseline.
    """

    if require_install_baseline and not installed_at:
        return "never_seen"
    last_before = state.get("last_before") if _at_or_after(state.get("last_before"), installed_at) else None
    last_after = state.get("last_after") if _at_or_after(state.get("last_after"), installed_at) else None
    last_error = state.get("last_error_message") if _at_or_after(state.get("last_error_at"), installed_at) else None
    last_error_at = state.get("last_error_at") if last_error else None
    # An older error must not hide a later completed turn.  ISO-8601 values
    # sort chronologically, and missing timestamps are treated conservatively.
    if last_error and (not last_after or not last_error_at or str(last_error_at) >= str(last_after)):
        return "error"
    if last_after:
        return "active"
    if last_before:
        return "before_only"
    return "never_seen"


def _template_contract(config: AppConfig, agent_id: str, registry: dict[str, object]) -> dict[str, object]:
    if not registry:
        return {
            "state": "not_installed", "current": False, "next_action": f"meta-memory install-agent {agent_id}",
            "contract_changed": False, "template_changed": False, "local_drift": False,
        }
    from .skill_installer import _upgrade_row

    row = _upgrade_row(config, agent_id, registry)
    state = str(row.get("template_contract_state") or row.get("status") or "unknown")
    return {
        "state": state,
        "current": bool(row.get("template_contract_current", state == "current")),
        "next_action": row.get("next_action"),
        "contract_changed": bool(row.get("contract_changed")),
        "template_changed": bool(row.get("template_changed")),
        "local_drift": bool(row.get("local_drift")),
        "host_template_changed": bool(row.get("host_template_changed")),
        "host_local_drift": bool(row.get("host_local_drift")),
        "launcher_contract_changed": bool(row.get("launcher_contract_changed")),
        "launcher_local_drift": bool(row.get("launcher_local_drift")),
        "installed_at": row.get("installed_at"),
        "skill_contract_version": row.get("skill_contract_version"),
        "current_contract_version": row.get("current_contract_version"),
    }


def _detection(config: AppConfig, agent_id: str, registry: dict[str, object]) -> tuple[bool | None, str]:
    from .agent_specs import BUILTIN_AGENT_IDS, detect_agent

    if agent_id in BUILTIN_AGENT_IDS:
        return detect_agent(agent_id), "host_directory_or_executable"
    if "host_detected_at_install" in registry:
        return bool(registry.get("host_detected_at_install")), "recorded_at_install"
    if registry:
        return None, "not_observable_for_legacy_custom_integration"
    return False, "not_installed"


def _one_status(config: AppConfig, *, agent_id: str, project, verbose: bool) -> dict[str, object]:
    from .spool import pending_dir

    registry = _registry(config, agent_id)
    state = _state(config, agent_id=agent_id, workspace_id=project.workspace_id)
    try:
        identity_warnings = project_identity_warnings(config, agent_id=agent_id, project=project)
    except Exception:
        identity_warnings = []

    default_skill, default_host = _builtin_paths(agent_id)
    launcher = Path(str(registry.get("launcher") or (Path(config.path).expanduser().resolve().parent / "bin" / f"meta-memory-{agent_id}{'.cmd' if os.name == 'nt' else ''}")))
    skill = Path(str(registry.get("skill") or default_skill)) if (registry.get("skill") or default_skill) else None
    host_value = registry.get("host_instruction") if registry else default_host
    host = Path(str(host_value)) if host_value else None
    host_state = _host_instruction_state(host)
    contract = _template_contract(config, agent_id, registry)
    detected, detection_basis = _detection(config, agent_id, registry)
    configured = str(Path(config.path).expanduser().resolve())
    store = str(Path(config.store).expanduser().resolve())
    skill_ok = bool(skill and skill.is_file())
    launcher_ok = launcher.is_file()
    host_ok = host_state in {"not_required", "managed_block_present"}
    files_installed = skill_ok and launcher_ok and host_ok
    raw_verified = bool(registry.get("launcher_verified"))
    verification_status = str(registry.get("launcher_verification_status") or "not_checked")
    if raw_verified and not bool(contract.get("current")):
        verification_state = "stale"
    elif raw_verified and registry.get("verified_at"):
        verification_state = "verified"
    elif raw_verified:
        # Legacy registries may predate timestamps.  Do not silently call an
        # un-dated assertion a fresh verification.
        verification_state = "recorded_without_timestamp"
    elif verification_status == "not_checked":
        verification_state = "not_checked"
    else:
        verification_state = "not_verified"
    launcher_verified = verification_state == "verified"
    lifecycle = _lifecycle_state(
        state,
        installed_at=registry.get("installed_at"),
        require_install_baseline=bool(registry),
    )
    lifecycle_observed = lifecycle != "never_seen"
    shared_config = str(registry.get("config") or configured) == configured
    shared_store = str(registry.get("store") or store) == store
    if not files_installed:
        integration_state = "not_installed" if not registry and not (skill_ok or launcher_ok) else "partial_install"
    elif not bool(contract.get("current")):
        integration_state = "needs_sync"
    elif not launcher_verified:
        integration_state = "needs_verification"
    elif lifecycle == "active":
        integration_state = "ready"
    elif lifecycle == "error":
        integration_state = "error"
    else:
        integration_state = "needs_activation"

    result: dict[str, object] = {
        "status": "ok",
        "agent": agent_id,
        # Host detection is independent from installation.  For legacy custom
        # integrations it can be unknown rather than falsely claimed true.
        "detected": detected,
        "detection_basis": detection_basis,
        "installed": files_installed,  # Backward-compatible alias.
        "files_installed": files_installed,
        "installation_state": "installed" if files_installed else "partial" if (skill_ok or launcher_ok or host_state == "managed_block_present") else "not_installed",
        "skill": "ok" if skill_ok else "not_found",
        "launcher": "ok" if launcher_ok else "not_found",
        "host_instruction": host_state,
        "shared_config": shared_config,
        "shared_store": shared_store,
        "template_contract": contract,
        "template_contract_current": bool(contract.get("current")),
        "launcher_verified": launcher_verified,
        "launcher_verification": {
            "state": verification_state,
            "status": verification_status,
            "verified_at": registry.get("verified_at"),
            "checked_at": registry.get("verification_checked_at"),
            "detail": registry.get("launcher_verification_detail") or None,
            "scope": LAUNCHER_VERIFICATION_SCOPE,
        },
        "lifecycle_state": lifecycle,
        "host_lifecycle_observed": lifecycle_observed,
        "host_lifecycle_scope": LIFECYCLE_OBSERVATION_SCOPE,
        "lifecycle_observation_since": registry.get("installed_at") if registry else None,
        "integration_state": integration_state,
        "ready_for_automatic_memory": integration_state == "ready" and shared_config and shared_store,
        "project": project.project_id,
        "last_before": state.get("last_before"),
        "last_after": state.get("last_after"),
        "last_retrieval_count": int(state.get("last_retrieval_count") or 0),
        "last_retrieval_ms": int(state.get("last_retrieval_duration_ms") or 0),
        "unfinished_turns": int(state["unfinished_turns"]),
        "pending_spool": len(list(pending_dir(config).glob("*.json"))) if pending_dir(config).is_dir() else 0,
        "last_error": state.get("last_error_message"),
        "validation_scope": {
            "files": "Checks the local Skill, launcher, and required managed host-instruction block.",
            "launcher": LAUNCHER_VERIFICATION_SCOPE,
            "template_contract": "Compares installed contract/template hashes and local file hashes with the current package templates.",
            "host_lifecycle": LIFECYCLE_OBSERVATION_SCOPE,
        },
    }
    if verbose:
        result["details"] = {
            "agent_id": agent_id, "integration_type": registry.get("integration_type", "unknown"),
            "launcher_path": str(launcher), "skill_path": str(skill) if skill else None,
            "host_instruction_file": str(host) if host else None, "config_path": configured, "store_path": store,
            "registry_path": str(_registry_path(config, agent_id)), "installed_at": registry.get("installed_at"),
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


def agent_status(
    config: AppConfig,
    *,
    agent_id: str,
    all_agents: bool = False,
    installed_default: bool = False,
    project_name: str = "auto",
    start: str | Path | None = None,
    verbose: bool = False,
) -> dict[str, object]:
    project = resolve_project(config, project_name, start)
    known = set(_known_agents(config, workspace_id=project.workspace_id))
    if all_agents:
        from .agent_specs import BUILTIN_AGENT_IDS, detect_agent

        known.update(name for name in BUILTIN_AGENT_IDS if detect_agent(name))
    agents = sorted(known) if (all_agents or installed_default) and known else [agent_id]
    rows = [_one_status(config, agent_id=name, project=project, verbose=verbose) for name in agents]
    grouped = all_agents or installed_default
    return {
        "status": "ok", "project": project.project_id, "agents": rows if grouped else [],
        **({} if grouped else rows[0]),
    }


def verify_agent(config: AppConfig, *, agent_id: str, project_name: str = "auto", start: str | Path | None = None) -> dict[str, object]:
    """Probe the launcher and say exactly what that probe does *not* prove."""

    bootstrap()
    from _common import ensure_store_ready
    from .skill_installer import _record_launcher_verification, _verify_launcher

    project = resolve_project(config, project_name, start)
    registry = _registry(config, agent_id)
    launcher = Path(str(registry.get("launcher") or (Path(config.path).expanduser().resolve().parent / "bin" / f"meta-memory-{agent_id}{'.cmd' if os.name == 'nt' else ''}")))
    verified, cli_status, detail = _verify_launcher(launcher) if launcher.is_file() else (False, "not_found", "launcher is missing")
    if registry:
        _record_launcher_verification(
            config,
            agent_id=agent_id,
            verified=verified,
            status=cli_status,
            detail=detail,
        )
    store = ensure_store_ready(Path(config.store))
    value = _one_status(config, agent_id=agent_id, project=project, verbose=True)
    lifecycle = str(value.get("lifecycle_state") or "never_seen")
    return {
        "status": "ok" if verified else "error", "agent": agent_id, "launcher_verified": verified,
        "launcher_status": cli_status, "detail": detail, "store": store, "project": project.project_id,
        "verification_scope": LAUNCHER_VERIFICATION_SCOPE,
        "host_lifecycle_state": lifecycle,
        "host_lifecycle_observed": bool(value.get("host_lifecycle_observed")),
        "activation_required": lifecycle in {"never_seen", "before_only"},
        "activation_next_step": (
            "Start one real conversation through the host, then run meta-memory agent status --all --verbose."
            if lifecycle in {"never_seen", "before_only"} else None
        ),
        "runtime": value,
    }
