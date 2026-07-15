"""Small, explicit configuration get/set surface for the public CLI."""
from __future__ import annotations

from typing import Callable

from .config import AppConfig, _bool, normalize_history_scope, save_config


def _boolean(value: str) -> bool:
    parsed = _bool(value, False)
    if str(value).strip().casefold() not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        raise ValueError("Boolean values must be true/false, yes/no, on/off, or 1/0.")
    return parsed


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


_FIELDS: dict[str, tuple[str, Callable[[str], object]]] = {
    "dream.heartbeat_enabled": ("dream_heartbeat_enabled", _boolean),
    "dream.heartbeat_interval_minutes": ("dream_heartbeat_interval_minutes", _heartbeat_interval),
    "dream.heartbeat_max_scopes": ("dream_heartbeat_max_scopes", _positive),
    "dream.heartbeat_max_jobs": ("dream_heartbeat_max_jobs", _positive),
    "dream.deep_enabled": ("dream_deep_enabled", _boolean),
    "dream.deep_schedule": ("dream_deep_schedule", str),
    "dream.deep_scan_days": ("dream_deep_scan_days", _positive),
    "history.scope": ("history_scope", normalize_history_scope),
    "history.allow_detail": ("history_allow_detail", _boolean),
    "history.detail_max_sessions": ("history_detail_max_sessions", _positive),
    "history.detail_max_turns": ("history_detail_max_turns", _positive),
    "history.detail_max_chars": ("history_detail_max_chars", _positive),
    "history.tool_summary_max_chars": ("history_tool_summary_max_chars", _positive),
    "turns.unfinished_warning_minutes": ("turns_unfinished_warning_minutes", _positive),
    "turns.abandon_after_minutes": ("turns_abandon_after_minutes", _positive),
}


def get_config_value(config: AppConfig, key: str) -> dict[str, object]:
    normalized = str(key or "").strip().casefold()
    try:
        attribute, _ = _FIELDS[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported configuration key: {key}") from exc
    return {"status": "ok", "key": normalized, "value": getattr(config, attribute)}


def set_config_value(config: AppConfig, key: str, value: str) -> dict[str, object]:
    normalized = str(key or "").strip().casefold()
    try:
        attribute, parser = _FIELDS[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported configuration key: {key}") from exc
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
    save_config(config)
    return {"status": "ok", "key": normalized, "value": getattr(config, attribute), "config": str(config.path)}
