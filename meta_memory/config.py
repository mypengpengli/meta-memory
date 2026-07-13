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
    auto_memory: bool = True
    memory_mode: str = "automatic"
    default_project: str = "general"
    search_depth: str = "auto"
    maintenance_enabled: bool = False
    maintenance_interval_minutes: int = 5
    dream_enabled: bool = False
    dream_schedule: str = "23:30"
    dream_scan_days: int = 7
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


def _value(data: dict[str, Any], group: str, name: str, default: Any) -> Any:
    section = data.get(group, {})
    return section.get(name, default) if isinstance(section, dict) else default


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path).expanduser() if path else default_config_path()
    if not config_path.exists():
        return AppConfig(path=config_path)
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    raw_projects = data.get("projects", {})
    projects = {str(key): slug(str(value), "general") for key, value in raw_projects.items()} if isinstance(raw_projects, dict) else {}
    name = str(_value(data, "user", "name", "User"))
    return AppConfig(
        path=config_path,
        user_name=name,
        user_id=slug(str(_value(data, "user", "id", slug(name, "user"))), "user"),
        store=Path(str(_value(data, "storage", "path", Path.home() / ".meta-memory" / "data"))).expanduser(),
        auto_memory=bool(_value(data, "behavior", "auto_memory", True)),
        memory_mode=str(_value(data, "behavior", "memory_mode", "automatic")),
        default_project=slug(str(_value(data, "behavior", "default_project", "general")), "general"),
        search_depth=str(_value(data, "behavior", "search_depth", "auto")),
        maintenance_enabled=bool(_value(data, "maintenance", "enabled", False)),
        maintenance_interval_minutes=max(1, int(_value(data, "maintenance", "interval_minutes", 5))),
        dream_enabled=bool(_value(data, "dream", "enabled", False)),
        dream_schedule=str(_value(data, "dream", "schedule", "23:30")),
        dream_scan_days=max(1, int(_value(data, "dream", "scan_days", 7))),
        top_k=max(1, int(_value(data, "retrieval", "top_k", 8))),
        embeddings=bool(_value(data, "retrieval", "embeddings", False)),
        http_api=bool(_value(data, "advanced", "http_api", False)),
        agent_private_memory=bool(_value(data, "advanced", "agent_private_memory", False)),
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
        "[behavior]", f"auto_memory = {str(config.auto_memory).lower()}", f"memory_mode = {_quote(config.memory_mode)}", f"default_project = {_quote(config.default_project)}", f"search_depth = {_quote(config.search_depth)}", "",
        "[maintenance]", f"enabled = {str(config.maintenance_enabled).lower()}", f"interval_minutes = {config.maintenance_interval_minutes}", "",
        "[dream]", f"enabled = {str(config.dream_enabled).lower()}", f"schedule = {_quote(config.dream_schedule)}", f"scan_days = {config.dream_scan_days}", "",
        "[retrieval]", f"top_k = {config.top_k}", f"embeddings = {str(config.embeddings).lower()}", "",
        "[advanced]", f"http_api = {str(config.http_api).lower()}", f"agent_private_memory = {str(config.agent_private_memory).lower()}", "",
        "[projects]",
    ]
    rows.extend(f"{_quote(path)} = {_quote(project)}" for path, project in sorted(config.projects.items()))
    config.path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return config.path
