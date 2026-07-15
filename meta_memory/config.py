"""Small, dependency-free user configuration for the public CLI."""
from __future__ import annotations

import os
import re
try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10 package install
    import tomli as tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def slug(value: str, fallback: str = "default") -> str:
    compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", value.casefold()).strip("-")
    return compact[:80] or fallback


def default_config_path() -> Path:
    override = os.environ.get("META_MEMORY_CONFIG", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".meta-memory" / "config.toml"


@dataclass
class AppConfig:
    path: Path
    user_name: str = "User"
    user_id: str = "user"
    store: Path = field(default_factory=lambda: Path.home() / ".meta-memory" / "data")
    memory_mode: str = "automatic"
    default_project: str = "general"
    search_depth: str = "auto"
    maintenance_enabled: bool = True
    maintenance_interval_minutes: int = 5
    dream_enabled: bool = True
    dream_schedule: str = "23:30"
    dream_scan_days: int = 7
    dream_provider: str = "deterministic"
    dream_command: str = ""
    dream_heartbeat_enabled: bool = True
    dream_heartbeat_interval_minutes: int = 10
    dream_heartbeat_max_scopes: int = 20
    dream_heartbeat_max_jobs: int = 50
    dream_deep_enabled: bool = True
    dream_deep_schedule: str = "23:30"
    dream_deep_scan_days: int = 7
    history_scope: str = "workspace-summary"
    history_allow_detail: bool = True
    history_detail_max_sessions: int = 3
    history_detail_max_turns: int = 8
    history_detail_max_chars: int = 12000
    history_tool_summary_max_chars: int = 1200
    session_auto_expire_hours: int = 8
    top_k: int = 8
    embeddings: bool = False
    http_api: bool = False
    agent_private_memory: bool = False
    projects: dict[str, str] = field(default_factory=dict)

    @property
    def subject_id(self) -> str:
        return f"person:{self.user_id}"

    @property
    def profile_id(self) -> str:
        return self.user_id

    @property
    def auto_memory(self) -> bool:
        """Compatibility shim for integrations that predate memory_mode."""
        return self.memory_mode == "automatic"


def _value(data: dict[str, Any], group: str, name: str, default: Any) -> Any:
    section = data.get(group, {})
    return section.get(name, default) if isinstance(section, dict) else default


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return default


def normalize_memory_mode(value: Any, *, fallback: str = "automatic") -> str:
    mode = str(value or "").strip().casefold()
    return mode if mode in {"manual", "conservative", "automatic"} else fallback


def normalize_search_depth(value: Any) -> str:
    depth = str(value or "").strip().casefold()
    return depth if depth in {"light", "normal", "deep", "auto"} else "auto"


def normalize_history_scope(value: Any) -> str:
    scope = str(value or "").strip().casefold()
    return scope if scope in {"agent", "workspace-summary"} else "workspace-summary"


def _interval(value: Any, default: int = 10) -> int:
    try:
        return min(10080, max(1, int(value)))
    except (TypeError, ValueError):
        return default


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path).expanduser() if path else default_config_path()
    if not config_path.exists():
        return AppConfig(path=config_path)
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    raw_projects = data.get("projects", {})
    projects = {str(key): slug(str(value), "general") for key, value in raw_projects.items()} if isinstance(raw_projects, dict) else {}
    name = str(_value(data, "user", "name", "User"))
    behavior = data.get("behavior", {}) if isinstance(data.get("behavior", {}), dict) else {}
    legacy_auto = _bool(behavior.get("auto_memory", True), True)
    configured_mode = behavior.get("memory_mode")
    memory_mode = normalize_memory_mode(
        configured_mode,
        fallback="automatic" if legacy_auto else "manual",
    )
    maintenance_section = data.get("maintenance", {}) if isinstance(data.get("maintenance", {}), dict) else {}
    dream_section = data.get("dream", {}) if isinstance(data.get("dream", {}), dict) else {}
    legacy_interval = maintenance_section.get("interval_minutes")
    heartbeat_interval = dream_section.get("heartbeat_interval_minutes", legacy_interval if legacy_interval is not None else 10)
    deep_enabled = _bool(dream_section.get("deep_enabled", dream_section.get("enabled", True)), True)
    deep_schedule = str(dream_section.get("deep_schedule", dream_section.get("schedule", "23:30")))
    deep_scan_days = max(1, int(dream_section.get("deep_scan_days", dream_section.get("scan_days", 7))))
    return AppConfig(
        path=config_path,
        user_name=name,
        user_id=slug(str(_value(data, "user", "id", slug(name, "user"))), "user"),
        store=Path(str(_value(data, "storage", "path", Path.home() / ".meta-memory" / "data"))).expanduser(),
        memory_mode=memory_mode,
        default_project=slug(str(_value(data, "behavior", "default_project", "general")), "general"),
        search_depth=normalize_search_depth(_value(data, "behavior", "search_depth", "auto")),
        maintenance_enabled=_bool(_value(data, "maintenance", "enabled", True), True),
        maintenance_interval_minutes=_interval(_value(data, "maintenance", "interval_minutes", 5), 5),
        dream_enabled=deep_enabled,
        dream_schedule=deep_schedule,
        dream_scan_days=deep_scan_days,
        dream_provider=str(_value(data, "dream", "provider", "deterministic")).strip().casefold() or "deterministic",
        dream_command=str(_value(data, "dream", "command", "")),
        dream_heartbeat_enabled=_bool(dream_section.get("heartbeat_enabled", _value(data, "maintenance", "enabled", True)), True),
        dream_heartbeat_interval_minutes=_interval(heartbeat_interval, 10),
        dream_heartbeat_max_scopes=max(1, int(dream_section.get("heartbeat_max_scopes", 20))),
        dream_heartbeat_max_jobs=max(1, int(dream_section.get("heartbeat_max_jobs", 50))),
        dream_deep_enabled=deep_enabled,
        dream_deep_schedule=deep_schedule,
        dream_deep_scan_days=deep_scan_days,
        history_scope=normalize_history_scope(_value(data, "history", "scope", "workspace-summary")),
        history_allow_detail=_bool(_value(data, "history", "allow_detail", True), True),
        history_detail_max_sessions=max(1, int(_value(data, "history", "detail_max_sessions", 3))),
        history_detail_max_turns=max(1, int(_value(data, "history", "detail_max_turns", 8))),
        history_detail_max_chars=max(256, int(_value(data, "history", "detail_max_chars", 12000))),
        history_tool_summary_max_chars=max(120, int(_value(data, "history", "tool_summary_max_chars", 1200))),
        session_auto_expire_hours=max(1, int(_value(data, "session", "auto_expire_hours", 8))),
        top_k=max(1, int(_value(data, "retrieval", "top_k", 8))),
        embeddings=_bool(_value(data, "retrieval", "embeddings", False), False),
        http_api=_bool(_value(data, "advanced", "http_api", False), False),
        agent_private_memory=_bool(_value(data, "advanced", "agent_private_memory", False), False),
        projects=projects,
    )


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_config(config: AppConfig) -> Path:
    config.path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        "# Meta Memory user configuration. Internal database scope fields stay hidden.",
        "[user]", f"name = {_quote(config.user_name)}", f"id = {_quote(config.user_id)}", "",
        "[storage]", f"path = {_quote(str(config.store))}", "",
        "[behavior]", f"memory_mode = {_quote(normalize_memory_mode(config.memory_mode))}", f"default_project = {_quote(config.default_project)}", f"search_depth = {_quote(normalize_search_depth(config.search_depth))}", "",
        "[session]", f"auto_expire_hours = {max(1, int(config.session_auto_expire_hours))}", "",
        "[maintenance]", f"enabled = {str(config.maintenance_enabled).lower()}", f"interval_minutes = {config.maintenance_interval_minutes}", "",
        "[dream]", f"enabled = {str(config.dream_deep_enabled).lower()}", f"heartbeat_enabled = {str(config.dream_heartbeat_enabled).lower()}", f"heartbeat_interval_minutes = {_interval(config.dream_heartbeat_interval_minutes, 10)}", f"heartbeat_max_scopes = {max(1, int(config.dream_heartbeat_max_scopes))}", f"heartbeat_max_jobs = {max(1, int(config.dream_heartbeat_max_jobs))}", f"deep_enabled = {str(config.dream_deep_enabled).lower()}", f"deep_schedule = {_quote(config.dream_deep_schedule)}", f"deep_scan_days = {max(1, int(config.dream_deep_scan_days))}", f"schedule = {_quote(config.dream_deep_schedule)}", f"scan_days = {max(1, int(config.dream_deep_scan_days))}", f"provider = {_quote(config.dream_provider)}", f"command = {_quote(config.dream_command)}", "",
        "[history]", f"scope = {_quote(normalize_history_scope(config.history_scope))}", f"allow_detail = {str(config.history_allow_detail).lower()}", f"detail_max_sessions = {max(1, int(config.history_detail_max_sessions))}", f"detail_max_turns = {max(1, int(config.history_detail_max_turns))}", f"detail_max_chars = {max(256, int(config.history_detail_max_chars))}", f"tool_summary_max_chars = {max(120, int(config.history_tool_summary_max_chars))}", "",
        "[retrieval]", f"top_k = {config.top_k}", f"embeddings = {str(config.embeddings).lower()}", "",
        "[advanced]", f"http_api = {str(config.http_api).lower()}", f"agent_private_memory = {str(config.agent_private_memory).lower()}", "",
        "[projects]",
    ]
    rows.extend(f"{_quote(path)} = {_quote(project)}" for path, project in sorted(config.projects.items()))
    config.path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return config.path
