"""Host-specific installation locations and lightweight detection rules."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentSpec:
    """Paths and executable names needed to install one host integration."""

    agent_id: str
    skill_dir: Path
    host_instruction_file: Path | None
    detection_paths: tuple[Path, ...]
    executable_names: tuple[str, ...]


BUILTIN_AGENT_IDS = ("claude-code", "codex", "openclaw")


def _home_path(home: str | Path | None = None) -> Path:
    return Path(home).expanduser() if home is not None else Path.home()


def agent_specs(
    *,
    home: str | Path | None = None,
    custom_skill_dir: str | Path | None = None,
) -> dict[str, AgentSpec]:
    """Build specs lazily so tests and portable homes remain deterministic."""

    root = _home_path(home)
    specs = {
        "codex": AgentSpec(
            agent_id="codex",
            skill_dir=root / ".codex" / "skills" / "meta-memory",
            host_instruction_file=root / ".codex" / "AGENTS.md",
            detection_paths=(root / ".codex",),
            executable_names=("codex",),
        ),
        "claude-code": AgentSpec(
            agent_id="claude-code",
            skill_dir=root / ".claude" / "skills" / "meta-memory",
            host_instruction_file=root / ".claude" / "CLAUDE.md",
            detection_paths=(root / ".claude",),
            executable_names=("claude", "claude-code"),
        ),
        "openclaw": AgentSpec(
            agent_id="openclaw",
            skill_dir=root / ".openclaw" / "skills" / "meta-memory",
            host_instruction_file=root / ".openclaw" / "AGENTS.md",
            detection_paths=(root / ".openclaw",),
            executable_names=("openclaw",),
        ),
    }
    if custom_skill_dir is not None:
        custom_root = Path(custom_skill_dir).expanduser()
        specs["custom"] = AgentSpec(
            agent_id="custom",
            skill_dir=custom_root / "meta-memory",
            host_instruction_file=custom_root.parent / "AGENTS.md",
            detection_paths=(custom_root,),
            executable_names=(),
        )
    return specs


def get_agent_spec(
    agent: str,
    *,
    home: str | Path | None = None,
    custom_skill_dir: str | Path | None = None,
) -> AgentSpec:
    name = str(agent or "").strip().casefold()
    if name == "custom" and custom_skill_dir is None:
        raise ValueError("custom agent installation requires --skill-dir")
    try:
        return agent_specs(home=home, custom_skill_dir=custom_skill_dir)[name]
    except KeyError as exc:
        supported = ", ".join((*BUILTIN_AGENT_IDS, "custom"))
        raise ValueError(f"Unsupported agent: {agent}. Supported agents: {supported}") from exc


def detect_agent(
    agent: str,
    *,
    home: str | Path | None = None,
    custom_skill_dir: str | Path | None = None,
) -> bool:
    """Return true for an existing host directory or a visible executable."""

    spec = get_agent_spec(agent, home=home, custom_skill_dir=custom_skill_dir)
    return any(path.exists() for path in spec.detection_paths) or any(
        shutil.which(executable) is not None for executable in spec.executable_names
    )
