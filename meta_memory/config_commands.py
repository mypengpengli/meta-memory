"""Small, explicit configuration get/set surface for the public CLI."""
from __future__ import annotations

from typing import Callable

from .config import AppConfig, _bool, normalize_history_scope, normalize_memory_mode, normalize_search_depth, save_config


def _boolean(value: str) -> bool:
    parsed = _bool(value, False)
    if str(value).strip().casefold() not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        raise ValueError("Boolean values must be true/false, yes/no, on/off, or 1/0.")
    return parsed


def _memory_mode(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in {"automatic", "conservative", "manual"}:
        raise ValueError("behavior.memory_mode must be automatic, conservative, or manual.")
    return normalize_memory_mode(normalized)


def _search_depth(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in {"light", "normal", "deep", "auto"}:
        raise ValueError("behavior.search_depth must be light, normal, deep, or auto.")
    return normalize_search_depth(normalized)


def _history_scope(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in {"agent", "workspace-summary"}:
        raise ValueError("history.scope must be agent or workspace-summary.")
    return normalize_history_scope(normalized)


def _positive(value: str) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("Configuration value must be a positive integer.") from exc


def _heartbeat_interval(value: str) -> int:
    try:
        interval = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("dream.heartbeat_interval_minutes must be an integer from 1 to 10080.") from exc
    if not 1 <= interval <= 10080:
        raise ValueError("dream.heartbeat_interval_minutes must be from 1 to 10080.")
    return interval


def _candidate_limit(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("retrieval.candidate_limit must be an integer from 24 to 512.") from exc
    if not 24 <= parsed <= 512:
        raise ValueError("retrieval.candidate_limit must be from 24 to 512.")
    return parsed


def _log_max_bytes(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("scheduler.log_max_bytes must be an integer from 65536 upward.") from exc
    if parsed < 65536:
        raise ValueError("scheduler.log_max_bytes must be at least 65536.")
    return parsed


def _log_keep_files(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("scheduler.log_keep_files must be an integer from 1 to 20.") from exc
    if not 1 <= parsed <= 20:
        raise ValueError("scheduler.log_keep_files must be from 1 to 20.")
    return parsed


_FIELDS: dict[str, tuple[str, Callable[[str], object], str, bool]] = {
    "behavior.memory_mode": ("memory_mode", _memory_mode, "Automatic, conservative, or manual memory extraction.", False),
    "behavior.search_depth": ("search_depth", _search_depth, "Default retrieval depth: light, normal, deep, or auto.", False),
    "behavior.default_project": ("default_project", str, "Fallback project name outside a bound directory.", False),
    "maintenance.enabled": ("maintenance_enabled", _boolean, "Legacy maintenance switch, kept in sync with the Dream heartbeat.", True),
    "maintenance.interval_minutes": ("maintenance_interval_minutes", _heartbeat_interval, "Legacy maintenance interval, kept in sync with the Dream heartbeat.", True),
    "dream.heartbeat_enabled": ("dream_heartbeat_enabled", _boolean, "Enable lightweight incremental memory maintenance.", True),
    "dream.heartbeat_interval_minutes": ("dream_heartbeat_interval_minutes", _heartbeat_interval, "Minutes between heartbeat runs (1–10080).", True),
    "dream.heartbeat_max_scopes": ("dream_heartbeat_max_scopes", _positive, "Maximum dirty scopes processed in one heartbeat.", False),
    "dream.heartbeat_max_jobs": ("dream_heartbeat_max_jobs", _positive, "Maximum review jobs processed in one heartbeat.", False),
    "dream.deep_enabled": ("dream_deep_enabled", _boolean, "Enable scheduled deep Dream synthesis.", True),
    "dream.deep_schedule": ("dream_deep_schedule", str, "Daily deep Dream time in HH:MM local time.", True),
    "dream.deep_scan_days": ("dream_deep_scan_days", _positive, "Recent source-evidence window for deep Dream.", False),
    "dream.provider": ("dream_provider", str, "Configured Dream synthesis provider.", False),
    "dream.command": ("dream_command", str, "Optional external command used by the Dream provider.", False),
    "history.scope": ("history_scope", _history_scope, "Agent-only detail or workspace completed-session summaries.", False),
    "history.allow_detail": ("history_allow_detail", _boolean, "Allow explicit bounded session-detail reads.", False),
    "history.detail_max_sessions": ("history_detail_max_sessions", _positive, "Maximum sessions returned by a detailed history read.", False),
    "history.detail_max_turns": ("history_detail_max_turns", _positive, "Maximum completed turns per detailed history read.", False),
    "history.detail_max_chars": ("history_detail_max_chars", _positive, "Maximum characters returned by a detailed history read.", False),
    "history.tool_summary_max_chars": ("history_tool_summary_max_chars", _positive, "Maximum tool-summary characters in history detail.", False),
    "turns.unfinished_warning_minutes": ("turns_unfinished_warning_minutes", _positive, "Age at which an unfinished turn appears as a warning.", False),
    "turns.abandon_after_minutes": ("turns_abandon_after_minutes", _positive, "Age at which an inactive turn becomes recoverable.", False),
    "session.auto_expire_hours": ("session_auto_expire_hours", _positive, "Lifetime of automatically derived local sessions.", False),
    "retrieval.top_k": ("top_k", _positive, "Default number of recalled memory results.", False),
    "retrieval.candidate_limit": ("retrieval_candidate_limit", _candidate_limit, "Maximum SQL/Python candidates considered before final retrieval ranking.", False),
    "retrieval.embeddings": ("embeddings", _boolean, "Use optional embedding retrieval when configured.", False),
    "retention.operational_days": ("retention_operational_days", _positive, "Days to retain completed operational queue and runtime records.", False),
    "retention.retrieval_days": ("retention_retrieval_days", _positive, "Days to retain detailed retrieval telemetry.", False),
    "retention.dream_report_days": ("retention_dream_report_days", _positive, "Days to retain per-run Dream reports before rollup/pruning.", False),
    "maintenance.compact_enabled": ("maintenance_compact_enabled", _boolean, "Allow periodic SQLite optimization/compaction during maintenance.", False),
    "scheduler.log_max_bytes": ("scheduler_log_max_bytes", _log_max_bytes, "Rotate a scheduler log when it reaches this size in bytes.", False),
    "scheduler.log_keep_files": ("scheduler_log_keep_files", _log_keep_files, "Number of rotated scheduler log files to retain (1–20).", False),
}


def _field(key: str) -> tuple[str, Callable[[str], object], str, bool]:
    normalized = str(key or "").strip().casefold()
    try:
        return _FIELDS[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported configuration key: {key}") from exc


def get_config_value(config: AppConfig, key: str) -> dict[str, object]:
    normalized = str(key or "").strip().casefold()
    attribute, _, description, affects_schedule = _field(normalized)
    return {"status": "ok", "key": normalized, "value": getattr(config, attribute), "description": description, "affects_schedule": affects_schedule}


def list_config_values(config: AppConfig) -> dict[str, object]:
    items = [
        {
            "key": key, "value": getattr(config, attribute), "description": description,
            "affects_schedule": affects_schedule,
        }
        for key, (attribute, _parser, description, affects_schedule) in sorted(_FIELDS.items())
    ]
    return {"status": "ok", "config": str(config.path), "items": items, "returned": len(items)}


def describe_config_value(config: AppConfig, key: str) -> dict[str, object]:
    return get_config_value(config, key)


def _schedule_is_installed(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if bool(value.get("managed_block")):
        return True
    tasks = value.get("tasks")
    if isinstance(tasks, list):
        return any(bool(item.get("installed")) for item in tasks if isinstance(item, dict))
    return bool(value.get("launcher_exists")) and bool(value.get("expected"))


def _refresh_schedule(config: AppConfig, *, force: bool) -> dict[str, object]:
    """Refresh an already-installed schedule, or install only on --apply.

    Configuration must not become invalid merely because a platform scheduler
    is unavailable in a temporary shell.  The saved value remains authoritative
    and the response tells the user exactly whether refresh happened.
    """
    try:
        from .scheduler import schedule_install, schedule_status

        previous = schedule_status(config)
        if force or _schedule_is_installed(previous):
            return {"status": "refreshed", "previous": previous, "result": schedule_install(config)}
        return {"status": "not_installed", "previous": previous, "next_action": "meta-memory schedule install"}
    except (OSError, RuntimeError) as exc:
        return {"status": "warning", "error": str(exc), "next_action": "meta-memory schedule install"}


def set_config_value(config: AppConfig, key: str, value: str, *, apply_schedule: bool = False) -> dict[str, object]:
    normalized = str(key or "").strip().casefold()
    attribute, parser, _description, affects_schedule = _field(normalized)
    parsed = parser(str(value))
    setattr(config, attribute, parsed)
    # Keep the historical fields written by older callers in sync with their
    # documented Dream replacements.
    if attribute == "dream_heartbeat_interval_minutes":
        config.maintenance_interval_minutes = int(parsed)
    if attribute == "dream_heartbeat_enabled":
        config.maintenance_enabled = bool(parsed)
    if attribute == "dream_deep_enabled":
        config.dream_enabled = bool(parsed)
    if attribute == "dream_deep_schedule":
        config.dream_schedule = str(parsed)
    if attribute == "dream_deep_scan_days":
        config.dream_scan_days = int(parsed)
    if attribute == "maintenance_interval_minutes":
        config.dream_heartbeat_interval_minutes = int(parsed)
    if attribute == "maintenance_enabled":
        config.dream_heartbeat_enabled = bool(parsed)
    save_config(config)
    result: dict[str, object] = {"status": "ok", "key": normalized, "value": getattr(config, attribute), "config": str(config.path)}
    if affects_schedule:
        result["schedule_refresh"] = _refresh_schedule(config, force=bool(apply_schedule))
    return result
