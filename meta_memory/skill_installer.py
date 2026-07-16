"""Install a verified, Agent-specific Meta Memory Skill and launcher."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Iterable

from .agent_specs import BUILTIN_AGENT_IDS, detect_agent, get_agent_spec
from .config import AppConfig, load_config, save_config


# This version changes only when the host/CLI hand-off contract changes.  It is
# intentionally independent from the package version so an upgrade can tell a
# user whether an installed Skill still speaks the current turn protocol.
SKILL_CONTRACT_VERSION = "turn-v3"
LAUNCHER_CONTRACT_VERSION = "launcher-v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _template_text(name: str) -> str:
    """Load installed package data, with a source-tree fallback for dev use."""

    try:
        return resources.files("meta_memory").joinpath("templates", name).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        return (Path(__file__).resolve().parent / "templates" / name).read_text(encoding="utf-8")


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _template_hash(name: str) -> str:
    return _text_hash(_template_text(name))


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


def _launcher_command(launcher: str | Path, *, windows: bool | None = None) -> str:
    """Return a copyable host-shell invocation for the generated launcher."""

    is_windows = os.name == "nt" if windows is None else windows
    text = str(launcher)
    if is_windows:
        # PowerShell does not invoke a quoted command path without its call
        # operator.  Single-quote and double embedded apostrophes so spaces,
        # dollar signs, and other path characters remain literal.
        return "& '" + text.replace("'", "''") + "'"
    return shlex.quote(text)


def _launcher_shell_help(launcher: str | Path, *, windows: bool | None = None) -> str:
    """Describe exact shell choices without assuming OS implies host shell."""

    is_windows = os.name == "nt" if windows is None else windows
    text = str(launcher)
    if not is_windows:
        return (
            f"- Host process/argv API: executable `{text}`; pass every following token as a separate argument.\n"
            f"- POSIX shell: use `{shlex.quote(text)}` as `<launcher>`."
        )
    powershell = _launcher_command(text, windows=True)
    cmd = f"call {_windows_quote(text)}"
    escaped = powershell.replace('"', '`"')
    return (
        f"- Host process/argv API (preferred): executable `{text}`; pass following tokens separately.\n"
        f"- PowerShell: use `{powershell}` as `<launcher>`.\n"
        f"- cmd.exe: use `{cmd}` as `<launcher>`.\n"
        "- Git Bash or another Windows shell: invoke through the host process API, configure that host to use "
        f"PowerShell/cmd.exe, or run `powershell.exe -NoProfile -NonInteractive -Command \"{escaped} <arguments>\"`."
    )


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


def _managed_block_hash(path: Path) -> str:
    """Hash only Meta Memory's managed host block, not the user's file."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"<!-- meta-memory:begin -->.*?<!-- meta-memory:end -->", text, re.S)
    return _text_hash(match.group(0)) if match else ""


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


def _agent_registry_path(config: AppConfig, agent_id: str) -> Path:
    return Path(config.path).expanduser().resolve().parent / "agents" / f"{agent_id}.json"


def _write_agent_registry(
    config: AppConfig,
    *,
    spec,
    launcher: Path,
    skill_file: Path,
    host_detected: bool,
    host_block_hash: str,
) -> Path:
    """Persist only installation metadata needed by local status/verify."""

    path = _agent_registry_path(config, spec.agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    from . import __version__

    payload = {
        "agent_id": spec.agent_id,
        "display_name": spec.display_name,
        "integration_type": spec.integration_type,
        "host_detected_at_install": bool(host_detected),
        "skill": str(skill_file),
        "host_instruction": str(spec.host_instruction_file) if spec.host_instruction_file else None,
        "launcher": str(launcher),
        "config": str(Path(config.path).expanduser().resolve()),
        "store": str(Path(config.store).expanduser().resolve()),
        "package_version": __version__,
        "app_version": __version__,
        "contract_version": SKILL_CONTRACT_VERSION,
        "skill_contract_version": SKILL_CONTRACT_VERSION,
        "launcher_contract_version": LAUNCHER_CONTRACT_VERSION,
        "skill_template_hash": _template_hash("skill.md.template"),
        "template_hash": _template_hash("skill.md.template"),
        "host_template_hash": _template_hash("host-instruction.md.template"),
        "host_block_hash": host_block_hash,
        "skill_content_hash": _file_hash(skill_file),
        "launcher_content_hash": _file_hash(launcher),
        # Installation and verification are deliberately separate facts.  A
        # written launcher proves only local files exist; it does not prove a
        # host has loaded its lifecycle instructions.
        "installed_at": _utc_now(),
        "verified_at": None,
        "verification_checked_at": None,
        "launcher_verified": False,
        "launcher_verification_status": "not_checked",
        "launcher_verification_detail": "",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _record_launcher_verification(
    config: AppConfig,
    *,
    agent_id: str,
    verified: bool,
    status: str,
    detail: str = "",
) -> dict[str, object]:
    """Persist the outcome of a real launcher probe without implying a hook.

    ``verified_at`` exists only for a successful probe.  A failed or skipped
    probe keeps the attempt timestamp separately so status can state exactly
    what has and has not been validated.
    """

    path = _agent_registry_path(config, agent_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        return {}
    now = _utc_now()
    payload["launcher_verified"] = bool(verified)
    payload["launcher_verification_status"] = str(status or ("ok" if verified else "unverified"))
    payload["launcher_verification_detail"] = str(detail or "")[:2000]
    payload["verification_checked_at"] = now
    payload["verified_at"] = now if verified else None
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return payload
    return payload


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
    if name != "custom" and (custom_skill_dir or custom_agent_id or custom_host_file or no_host_file):
        raise ValueError("--skill-dir, --agent-id, --host-file and --no-host-file are only valid for install-agent custom.")
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
        "launcher_command": _launcher_command(launcher),
        "launcher_shell_help": _launcher_shell_help(launcher),
        "config_path": str(Path(app_config.path).expanduser().resolve()),
    }
    spec.skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = spec.skill_dir / "SKILL.md"
    skill_file.write_text(_render_template("skill.md.template", **values), encoding="utf-8")
    host_block = ""
    if spec.host_instruction_file is not None:
        host_block = _render_template("host-instruction.md.template", **values)
        _upsert_block(spec.host_instruction_file, host_block)
    registry = _write_agent_registry(
        app_config,
        spec=spec,
        launcher=launcher,
        skill_file=skill_file,
        host_detected=detected,
        host_block_hash=_text_hash(host_block) if host_block else "",
    )

    launcher_created = launcher.is_file()
    cli_visible, memory_status, detail = False, "not_checked", ""
    verification_metadata: dict[str, object] = {
        "verification_checked_at": None,
        "verified_at": None,
    }
    if verify and launcher_created and config_visible:
        cli_visible, memory_status, detail = _verify_launcher(launcher)
        verification_metadata = _record_launcher_verification(
            app_config,
            agent_id=spec.agent_id,
            verified=cli_visible,
            status=memory_status,
            detail=detail,
        )
        if not cli_visible:
            warnings.append(f"Launcher verification failed: {detail}")
    elif verify:
        memory_status = "unverified"
        detail = "Launcher verification skipped because launcher or config is unavailable."
        verification_metadata = _record_launcher_verification(
            app_config,
            agent_id=spec.agent_id,
            verified=False,
            status=memory_status,
            detail=detail,
        )
        warnings.append("Launcher verification skipped because launcher or config is unavailable.")
    host_installed = spec.host_instruction_file is None or spec.host_instruction_file.is_file()
    skill_installed = skill_file.is_file()
    files_ready = bool(config_visible and launcher_created and skill_installed and host_installed)
    verification_ready = bool(verify and cli_visible)
    local_install_ready = bool(files_ready and verification_ready)
    if not files_ready:
        next_action = f"meta-memory agent sync {spec.agent_id}"
        activation_status = "blocked_on_installation"
    elif not verification_ready:
        next_action = f"meta-memory agent verify {spec.agent_id}"
        activation_status = "blocked_on_launcher_verification"
    else:
        next_action = "meta-memory agent status --all --verbose"
        activation_status = "awaiting_first_host_turn"
    manual_next_step = (
        f"Restart or start {spec.display_name}, complete one normal conversation, then run "
        "meta-memory agent status --all --verbose and confirm lifecycle_state is active."
    )
    return {
        # Installing files is not end-to-end activation.  Never return a green
        # top-level status until the caller has an explicit next step for the
        # host that must load and execute this Skill.
        "status": "needs_action",
        "installation_status": "ok" if files_ready else "incomplete",
        "agent": spec.agent_id,
        "agent_id": spec.agent_id,
        "display_name": spec.display_name,
        "integration_type": spec.integration_type,
        "agent_detected": detected,
        "skill_installed": skill_installed,
        "host_instruction_installed": host_installed,
        "launcher_created": launcher_created,
        "cli_visible": cli_visible,
        "config_visible": config_visible,
        "memory_status": memory_status,
        "launcher_verified": cli_visible,
        "verification_checked_at": verification_metadata.get("verification_checked_at"),
        "verified_at": verification_metadata.get("verified_at"),
        "launcher_verification_scope": "Runs the generated launcher’s status command. It validates the local launcher, configuration, and store path; it does not prove the host loaded its lifecycle instructions.",
        "next_action": next_action,
        "activation_required": True,
        "activation_status": activation_status,
        "manual_next_step": manual_next_step if local_install_ready else None,
        "warnings": warnings,
        # Retain the old result keys for callers that only displayed paths.
        "skill": str(skill_file),
        "host_instruction": str(spec.host_instruction_file) if spec.host_instruction_file else None,
        "launcher": str(launcher),
        "launcher_command": _launcher_command(launcher),
        "launcher_shell_help": _launcher_shell_help(launcher),
        "shared_config": str(Path(app_config.path).expanduser().resolve()),
        "shared_store": str(Path(app_config.store).expanduser().resolve()),
        "registry": str(registry),
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
    results: list[dict[str, object]] = []
    for agent in selected:
        is_custom = agent == "custom"
        try:
            results.append(
                install_agent(
                    agent,
                    config=config,
                    custom_skill_dir=custom_skill_dir if is_custom else None,
                    custom_agent_id=custom_agent_id if is_custom else None,
                    custom_host_file=custom_host_file if is_custom else None,
                    no_host_file=no_host_file if is_custom else False,
                    home=home,
                    python_executable=python_executable,
                    verify=verify,
                )
            )
        except (OSError, RuntimeError) as exc:
            results.append({
                "status": "error",
                "agent": custom_agent_id if agent == "custom" and custom_agent_id else agent,
                "error": str(exc),
                "next_action": "Retry the same install-agent command after resolving the reported filesystem or runtime error.",
            })
    return results


def _registry_payloads(config: AppConfig) -> dict[str, dict[str, object]]:
    directory = _agent_registry_path(config, "_").parent
    result: dict[str, dict[str, object]] = {}
    if not directory.is_dir():
        return result
    for path in directory.glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            result[path.stem] = value
    return result


def sync_agent(config: AppConfig, agent_id: str, *, verify: bool = True) -> dict[str, object]:
    """Regenerate one installed integration from the canonical template."""
    agent = str(agent_id or "").strip().casefold()
    registry = _registry_payloads(config).get(agent)
    if agent in BUILTIN_AGENT_IDS:
        return install_agent(agent, config=config, verify=verify)
    if not registry:
        raise ValueError("Custom Agent is not installed; provide its original custom skill directory with install-agent custom.")
    skill = Path(str(registry.get("skill") or ""))
    if not str(skill):
        raise ValueError("Installed custom Agent has no recorded Skill path.")
    # ``install_agent(custom)`` receives the directory before its
    # ``meta-memory`` child.  This reconstruction keeps old registries usable.
    custom_root = skill.parent.parent
    host = registry.get("host_instruction")
    return install_agent(
        "custom",
        config=config,
        custom_skill_dir=custom_root,
        custom_agent_id=agent,
        custom_host_file=str(host) if host else None,
        no_host_file=not bool(host),
        verify=verify,
    )


def sync_agents(
    config: AppConfig,
    *,
    agents: Iterable[str] = (),
    all_agents: bool = False,
    verify: bool = True,
) -> dict[str, object]:
    registered = _registry_payloads(config)
    selected = [str(item).strip().casefold() for item in agents if str(item).strip()]
    if all_agents:
        selected = sorted(registered)
    selected = list(dict.fromkeys(selected))
    if not selected:
        return {
            "status": "needs_action", "synced": [],
            "next_action": "meta-memory install-agent codex",
            "reason": "no_installed_agents",
        }
    results: list[dict[str, object]] = []
    for agent in selected:
        try:
            results.append(sync_agent(config, agent, verify=verify))
        except (OSError, ValueError, RuntimeError) as exc:
            results.append({"status": "error", "agent": agent, "error": str(exc)})
    errors = [item for item in results if str(item.get("status")) == "error"]
    pending = [item for item in results if str(item.get("status")) == "needs_action"]
    status = "partial" if errors else "needs_action" if pending else "ok"
    return {"status": status, "synced": results, "failed": len(errors), "needs_action": len(pending)}


def uninstall_agent(config: AppConfig, agent_id: str) -> dict[str, object]:
    """Remove only files and the managed host block owned by one integration."""
    agent = str(agent_id or "").strip().casefold()
    registry_path = _agent_registry_path(config, agent)
    registry = _registry_payloads(config).get(agent)
    if not registry:
        return {"status": "not_found", "agent": agent, "removed": []}
    removed: list[str] = []
    for key in ("launcher", "skill"):
        path = Path(str(registry.get(key) or ""))
        if path.is_file():
            path.unlink()
            removed.append(str(path))
    # A host instruction can be shared by custom integrations.  Remove its
    # managed block only once no other registry still refers to that host.
    host_text = str(registry.get("host_instruction") or "")
    others = _registry_payloads(config)
    host_is_shared = any(
        name != agent and str(value.get("host_instruction") or "") == host_text
        for name, value in others.items()
    )
    host = Path(host_text) if host_text else None
    if host and host.is_file() and not host_is_shared:
        expression = re.compile(r"(?:\n)?<!-- meta-memory:begin -->.*?<!-- meta-memory:end -->\s*", re.S)
        old = host.read_text(encoding="utf-8")
        updated = expression.sub("", old).rstrip() + ("\n" if old.strip() else "")
        if updated != old:
            host.write_text(updated, encoding="utf-8")
            removed.append(str(host))
    if registry_path.is_file():
        registry_path.unlink()
        removed.append(str(registry_path))
    return {"status": "ok", "agent": agent, "removed": removed, "host_block_retained": bool(host_is_shared)}


def _upgrade_row(config: AppConfig, agent: str, registry: dict[str, object]) -> dict[str, object]:
    skill = Path(str(registry.get("skill") or ""))
    launcher = Path(str(registry.get("launcher") or ""))
    host_text = str(registry.get("host_instruction") or "")
    host = Path(host_text) if host_text else None
    current_template = _template_hash("skill.md.template")
    current_host_template = _template_hash("host-instruction.md.template")
    expected_skill = str(registry.get("skill_content_hash") or "")
    actual_skill = _file_hash(skill)
    expected_launcher = str(registry.get("launcher_content_hash") or "")
    actual_launcher = _file_hash(launcher)
    expected_host_block = str(registry.get("host_block_hash") or "")
    actual_host_block = _managed_block_hash(host) if host is not None else ""
    missing = not skill.is_file() or not launcher.is_file() or bool(host is not None and not actual_host_block)
    skill_contract_changed = str(registry.get("skill_contract_version") or "") != SKILL_CONTRACT_VERSION
    launcher_contract_changed = str(registry.get("launcher_contract_version") or "") != LAUNCHER_CONTRACT_VERSION
    contract_changed = skill_contract_changed or launcher_contract_changed
    skill_template_changed = str(registry.get("skill_template_hash") or "") != current_template
    host_template_changed = bool(host is not None and str(registry.get("host_template_hash") or "") != current_host_template)
    template_changed = skill_template_changed or host_template_changed
    skill_drift = bool(expected_skill and actual_skill and expected_skill != actual_skill)
    launcher_drift = bool(expected_launcher and actual_launcher and expected_launcher != actual_launcher)
    host_drift = bool(host is not None and expected_host_block and actual_host_block and expected_host_block != actual_host_block)
    drifted = skill_drift or launcher_drift or host_drift
    state = "missing" if missing else "needs_sync" if contract_changed or template_changed else "drifted" if drifted else "up_to_date"
    return {
        "agent": agent, "status": state, "installed": not missing,
        "template_contract_state": "current" if state == "up_to_date" else state,
        "template_contract_current": state == "up_to_date",
        "skill_contract_version": registry.get("skill_contract_version"), "current_contract_version": SKILL_CONTRACT_VERSION,
        "launcher_contract_version": registry.get("launcher_contract_version"), "current_launcher_contract_version": LAUNCHER_CONTRACT_VERSION,
        "template_changed": template_changed, "contract_changed": contract_changed, "local_drift": drifted,
        "host_template_changed": host_template_changed, "host_local_drift": host_drift,
        "launcher_contract_changed": launcher_contract_changed, "launcher_local_drift": launcher_drift,
        "installed_at": registry.get("installed_at"), "verified_at": registry.get("verified_at"),
        "launcher_verified": bool(registry.get("launcher_verified")),
        "next_action": None if state == "up_to_date" else f"meta-memory agent sync {agent}",
    }


def agent_upgrade_status(config: AppConfig, *, agent_id: str = "", all_agents: bool = False) -> dict[str, object]:
    registries = _registry_payloads(config)
    selected = sorted(registries) if all_agents or not str(agent_id).strip() else [str(agent_id).strip().casefold()]
    rows = [
        _upgrade_row(config, agent, registries[agent]) if agent in registries else {
            "agent": agent, "status": "not_installed", "installed": False,
            "next_action": f"meta-memory install-agent {agent}",
        }
        for agent in selected
    ]
    if not rows:
        return {"status": "needs_action", "agents": [], "next_action": "meta-memory install-agent codex"}
    needs = [item for item in rows if item["status"] != "up_to_date"]
    return {"status": "needs_action" if needs else "ok", "agents": rows, "needs_sync": len(needs)}
