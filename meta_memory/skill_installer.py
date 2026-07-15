"""Install a verified, Agent-specific Meta Memory Skill and launcher."""
from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
import sys
from importlib import resources
from pathlib import Path
from typing import Iterable

from .agent_specs import BUILTIN_AGENT_IDS, detect_agent, get_agent_spec
from .config import AppConfig, load_config, save_config


def _template_text(name: str) -> str:
    """Load installed package data, with a source-tree fallback for dev use."""

    try:
        return resources.files("meta_memory").joinpath("templates", name).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        return (Path(__file__).resolve().parent / "templates" / name).read_text(encoding="utf-8")


def _render_template(name: str, **values: object) -> str:
    rendered = _template_text(name)
    for key, value in values.items():
        rendered = rendered.replace("{{ " + key + " }}", str(value))
    unresolved = re.findall(r"{{\s*[^}]+\s*}}", rendered)
    if unresolved:
        raise RuntimeError(f"Unresolved template variables in {name}: {', '.join(unresolved)}")
    return rendered


def _launcher_path(config: AppConfig, agent_id: str, *, windows: bool | None = None) -> Path:
    is_windows = os.name == "nt" if windows is None else windows
    root = Path(config.path).expanduser().resolve().parent / "bin"
    return root / f"meta-memory-{agent_id}{'.cmd' if is_windows else ''}"


def _windows_quote(value: str | Path) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _launcher_text(
    *,
    python_executable: str | Path,
    config_path: str | Path,
    agent_id: str,
    windows: bool | None = None,
) -> str:
    """Return a tiny launcher that never relies on a host's PATH or cwd."""

    is_windows = os.name == "nt" if windows is None else windows
    if is_windows:
        return (
            "@echo off\r\n"
            f"{_windows_quote(python_executable)} -m meta_memory.cli "
            f"--config {_windows_quote(config_path)} --agent-id {_windows_quote(agent_id)} %*\r\n"
        )
    return (
        "#!/bin/sh\n"
        f"exec {shlex.quote(str(python_executable))} -m meta_memory.cli "
        f"--config {shlex.quote(str(config_path))} --agent-id {shlex.quote(agent_id)} \"$@\"\n"
    )


def _write_launcher(
    config: AppConfig,
    *,
    agent_id: str,
    python_executable: str | Path | None = None,
    windows: bool | None = None,
) -> Path:
    is_windows = os.name == "nt" if windows is None else windows
    launcher = _launcher_path(config, agent_id, windows=is_windows)
    launcher.parent.mkdir(parents=True, exist_ok=True)
    executable = Path(python_executable or sys.executable).expanduser().resolve()
    launcher.write_text(
        _launcher_text(
            python_executable=executable,
            config_path=Path(config.path).expanduser().resolve(),
            agent_id=agent_id,
            windows=is_windows,
        ),
        encoding="utf-8",
        newline="" if is_windows else "\n",
    )
    if not is_windows:
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return launcher


def _upsert_block(path: Path, block: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    expression = re.compile(r"<!-- meta-memory:begin -->.*?<!-- meta-memory:end -->", re.S)
    updated = expression.sub(block, old) if expression.search(old) else (old.rstrip() + "\n\n" + block + "\n")
    path.write_text(updated, encoding="utf-8")


def _verify_launcher(launcher: Path, *, windows: bool | None = None) -> tuple[bool, str, str]:
    """Run ``launcher status`` and require a JSON health result."""

    is_windows = os.name == "nt" if windows is None else windows
    command = [str(launcher), "status"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Windows may require cmd.exe to launch a .cmd wrapper when Python is
        # embedded or associated differently from a normal console session.
        if not is_windows:
            return False, "unverified", str(exc)
        try:
            completed = subprocess.run(
                [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", f'"{launcher}" status'],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as fallback_exc:
            return False, "unverified", str(fallback_exc)
    output = completed.stdout.strip()
    if completed.returncode != 0:
        detail = (completed.stderr or output or f"exit {completed.returncode}").strip()
        return False, "unverified", detail
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return False, "unverified", "launcher status did not return JSON"
    if not isinstance(payload, dict):
        return False, "unverified", "launcher status returned a non-object JSON value"
    status = str(payload.get("status") or "unknown")
    if status != "ok":
        return False, status, str(payload.get("error") or "launcher status was not ok")
    return True, status, ""


def _ensure_config(config: AppConfig, warnings: list[str]) -> bool:
    path = Path(config.path).expanduser()
    if path.is_file():
        return True
    try:
        save_config(config)
    except OSError as exc:
        warnings.append(f"Could not create config file: {exc}")
        return False
    return path.is_file()


def install_agent(
    agent: str,
    *,
    config: AppConfig | None = None,
    custom_skill_dir: str | Path | None = None,
    custom_agent_id: str | None = None,
    custom_host_file: str | Path | None = None,
    no_host_file: bool = False,
    home: str | Path | None = None,
    python_executable: str | Path | None = None,
    verify: bool = True,
) -> dict[str, object]:
    """Install one explicit Agent integration, even if it was not detected."""

    app_config = config or load_config()
    name = str(agent or "").strip().casefold()
    if name != "custom" and (custom_agent_id or custom_host_file or no_host_file):
        raise ValueError("--agent-id, --host-file and --no-host-file are only valid for install-agent custom.")
    spec = get_agent_spec(
        name,
        home=home,
        custom_skill_dir=custom_skill_dir,
        custom_agent_id=custom_agent_id,
        custom_host_file=custom_host_file,
        no_host_file=no_host_file,
    )
    warnings: list[str] = []
    detected = detect_agent(
        name,
        home=home,
        custom_skill_dir=custom_skill_dir,
        custom_agent_id=custom_agent_id,
        custom_host_file=custom_host_file,
        no_host_file=no_host_file,
    )
    if not detected:
        warnings.append(f"Host '{name}' was not detected; installed because it was explicitly requested.")
    config_visible = _ensure_config(app_config, warnings)
    launcher = _write_launcher(app_config, agent_id=spec.agent_id, python_executable=python_executable)
    values = {
        "agent_id": spec.agent_id,
        "launcher_path": str(launcher),
        "config_path": str(Path(app_config.path).expanduser().resolve()),
    }
    spec.skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = spec.skill_dir / "SKILL.md"
    skill_file.write_text(_render_template("skill.md.template", **values), encoding="utf-8")
    if spec.host_instruction_file is not None:
        _upsert_block(spec.host_instruction_file, _render_template("host-instruction.md.template", **values))

    launcher_created = launcher.is_file()
    cli_visible, memory_status = False, "not_checked"
    if verify and launcher_created and config_visible:
        cli_visible, memory_status, detail = _verify_launcher(launcher)
        if not cli_visible:
            warnings.append(f"Launcher verification failed: {detail}")
    elif verify:
        memory_status = "unverified"
        warnings.append("Launcher verification skipped because launcher or config is unavailable.")
    host_installed = spec.host_instruction_file is None or spec.host_instruction_file.is_file()
    return {
        "status": "ok",
        "agent": spec.agent_id,
        "agent_id": spec.agent_id,
        "display_name": spec.display_name,
        "integration_type": spec.integration_type,
        "agent_detected": detected,
        "skill_installed": skill_file.is_file(),
        "host_instruction_installed": host_installed,
        "launcher_created": launcher_created,
        "cli_visible": cli_visible,
        "config_visible": config_visible,
        "memory_status": memory_status,
        "launcher_verified": cli_visible,
        "warnings": warnings,
        # Retain the old result keys for callers that only displayed paths.
        "skill": str(skill_file),
        "host_instruction": str(spec.host_instruction_file) if spec.host_instruction_file else None,
        "launcher": str(launcher),
        "shared_config": str(Path(app_config.path).expanduser().resolve()),
        "shared_store": str(Path(app_config.store).expanduser().resolve()),
    }


def install_agents(
    agents: Iterable[str],
    *,
    config: AppConfig | None = None,
    custom_skill_dir: str | Path | None = None,
    custom_agent_id: str | None = None,
    custom_host_file: str | Path | None = None,
    no_host_file: bool = False,
    home: str | Path | None = None,
    python_executable: str | Path | None = None,
    verify: bool = True,
) -> list[dict[str, object]]:
    """Install explicit Agents plus only detected built-ins for ``all``."""

    values = list(dict.fromkeys(str(item).strip().casefold() for item in agents if str(item).strip()))
    requested_all = "all" in values
    explicit = [item for item in values if item != "all"]
    detected = (
        [agent for agent in BUILTIN_AGENT_IDS if detect_agent(agent, home=home)]
        if requested_all
        else []
    )
    selected = list(dict.fromkeys([*detected, *explicit]))
    return [
        install_agent(
            agent,
            config=config,
            custom_skill_dir=custom_skill_dir,
            custom_agent_id=custom_agent_id,
            custom_host_file=custom_host_file,
            no_host_file=no_host_file,
            home=home,
            python_executable=python_executable,
            verify=verify,
        )
        for agent in selected
    ]
