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
    return AppConfig(
        path=config_path,
        user_name=name,
        user_id=slug(str(_value(data, "user", "id", slug(name, "user"))), "user"),
        store=Path(str(_value(data, "storage", "path", Path.home() / ".meta-memory" / "data"))).expanduser(),
        memory_mode=memory_mode,
        default_project=slug(str(_value(data, "behavior", "default_project", "general")), "general"),
        search_depth=normalize_search_depth(_value(data, "behavior", "search_depth", "auto")),
        maintenance_enabled=_bool(_value(data, "maintenance", "enabled", True), True),
        maintenance_interval_minutes=max(1, int(_value(data, "maintenance", "interval_minutes", 5))),
        dream_enabled=_bool(_value(data, "dream", "enabled", True), True),
        dream_schedule=str(_value(data, "dream", "schedule", "23:30")),
        dream_scan_days=max(1, int(_value(data, "dream", "scan_days", 7))),
        dream_provider=str(_value(data, "dream", "provider", "deterministic")).strip().casefold() or "deterministic",
        dream_command=str(_value(data, "dream", "command", "")),
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
        "[dream]", f"enabled = {str(config.dream_enabled).lower()}", f"schedule = {_quote(config.dream_schedule)}", f"scan_days = {config.dream_scan_days}", f"provider = {_quote(config.dream_provider)}", f"command = {_quote(config.dream_command)}", "",
        "[retrieval]", f"top_k = {config.top_k}", f"embeddings = {str(config.embeddings).lower()}", "",
        "[advanced]", f"http_api = {str(config.http_api).lower()}", f"agent_private_memory = {str(config.agent_private_memory).lower()}", "",
        "[projects]",
    ]
    rows.extend(f"{_quote(path)} = {_quote(project)}" for path, project in sorted(config.projects.items()))
    config.path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return config.path
