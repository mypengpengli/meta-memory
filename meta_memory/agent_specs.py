"""Host-specific installation locations and lightweight detection rules."""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentSpec:
    """Paths and executable names needed to install one host integration."""

    agent_id: str
    display_name: str
    skill_dir: Path
    host_instruction_file: Path | None
    detection_paths: tuple[Path, ...]
    executable_names: tuple[str, ...]
    builtin: bool
    integration_type: str


BUILTIN_AGENT_IDS = ("claude-code", "codex", "openclaw")
_AGENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_RESERVED_AGENT_IDS = {"system", "meta-memory"}


def normalize_agent_id(agent_id: str, *, allow_builtin: bool = True) -> str:
    """Validate a portable Agent identity without silently changing it.

    Agent ids are persisted in provenance and launcher file names, so accepting
    path-like or lossy values would make different hosts appear to be the same
    integration.  We only normalize case; callers receive a useful error for
    every other invalid form.
    """

    value = str(agent_id or "").strip().casefold()
    if not _AGENT_ID_PATTERN.fullmatch(value):
        raise ValueError("Agent ID must be 1-64 lowercase letters, digits, '.', '_' or '-'.")
    if value in _RESERVED_AGENT_IDS:
        raise ValueError(f"Agent ID '{value}' is reserved.")
    normalized_shape = re.sub(r"[._-]+", "-", value)
    builtin_shapes = {re.sub(r"[._-]+", "-", item) for item in BUILTIN_AGENT_IDS}
    if not allow_builtin and (value in BUILTIN_AGENT_IDS or normalized_shape in builtin_shapes):
        raise ValueError("Use the built-in Agent name directly; custom Agent IDs may not impersonate a built-in.")
    return value


def _home_path(home: str | Path | None = None) -> Path:
    return (Path(home).expanduser() if home is not None else Path.home()).resolve()


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
            display_name="Codex",
            skill_dir=root / ".codex" / "skills" / "meta-memory",
            host_instruction_file=root / ".codex" / "AGENTS.md",
            detection_paths=(root / ".codex",),
            executable_names=("codex",),
            builtin=True,
            integration_type="builtin-skill",
        ),
        "claude-code": AgentSpec(
            agent_id="claude-code",
            display_name="Claude Code",
            skill_dir=root / ".claude" / "skills" / "meta-memory",
            host_instruction_file=root / ".claude" / "CLAUDE.md",
            detection_paths=(root / ".claude",),
            executable_names=("claude", "claude-code"),
            builtin=True,
            integration_type="builtin-skill",
        ),
        "openclaw": AgentSpec(
            agent_id="openclaw",
            display_name="OpenClaw",
            skill_dir=root / ".openclaw" / "skills" / "meta-memory",
            host_instruction_file=root / ".openclaw" / "AGENTS.md",
            detection_paths=(root / ".openclaw",),
            executable_names=("openclaw",),
            builtin=True,
            integration_type="builtin-skill",
        ),
    }
    if custom_skill_dir is not None:
        custom_root = Path(custom_skill_dir).expanduser().resolve()
        specs["custom"] = custom_agent_spec("custom", custom_root)
    return specs


def custom_agent_spec(
    agent_id: str,
    skill_dir: str | Path,
    host_file: str | Path | None = None,
    *,
    install_host_file: bool = True,
) -> AgentSpec:
    """Build a generic Skill integration for any CLI-capable Agent host."""

    normalized = normalize_agent_id(agent_id, allow_builtin=False)
    if skill_dir is None or (isinstance(skill_dir, str) and not skill_dir.strip()):
        raise ValueError("custom agent installation requires a non-empty --skill-dir")
    if isinstance(host_file, str) and not host_file.strip():
        raise ValueError("--host-file must be a non-empty path; use --no-host-file when it is not required")
    root = Path(skill_dir).expanduser().resolve()
    host = None if not install_host_file else (Path(host_file).expanduser().resolve() if host_file is not None else root.parent / "AGENTS.md")
    return AgentSpec(
        agent_id=normalized,
        display_name=normalized,
        skill_dir=root / "meta-memory",
        host_instruction_file=host,
        detection_paths=(root,),
        executable_names=(),
        builtin=False,
        integration_type="custom-skill",
    )


def get_agent_spec(
    agent: str,
    *,
    home: str | Path | None = None,
    custom_skill_dir: str | Path | None = None,
    custom_agent_id: str | None = None,
    custom_host_file: str | Path | None = None,
    no_host_file: bool = False,
) -> AgentSpec:
    name = str(agent or "").strip().casefold()
    if name == "custom":
        if custom_skill_dir is None:
            raise ValueError("custom agent installation requires --skill-dir")
        if not str(custom_agent_id or "").strip():
            raise ValueError("custom agent installation requires --agent-id")
        return custom_agent_spec(
            custom_agent_id,
            custom_skill_dir,
            custom_host_file,
            install_host_file=not no_host_file,
        )
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
    custom_agent_id: str | None = None,
    custom_host_file: str | Path | None = None,
    no_host_file: bool = False,
) -> bool:
    """Return true for an existing host directory or a visible executable."""

    spec = get_agent_spec(
        agent,
        home=home,
        custom_skill_dir=custom_skill_dir,
        custom_agent_id=custom_agent_id,
        custom_host_file=custom_host_file,
        no_host_file=no_host_file,
    )
    return any(path.exists() for path in spec.detection_paths) or any(
        shutil.which(executable) is not None for executable in spec.executable_names
    )
