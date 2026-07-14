"""Install, inspect, remove, and run safe platform-native local schedules."""
from __future__ import annotations

import hashlib
import os
import plistlib
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import AppConfig
from .scheduler_launcher import scheduler_launcher_path, scheduler_log_path, write_scheduler_launcher


_BEGIN = "# meta-memory:begin"
_END = "# meta-memory:end"


def _key(config: AppConfig) -> str:
    return hashlib.sha256(str(Path(config.path).expanduser().resolve()).encode("utf-8")).hexdigest()[:10]


def _task_names(config: AppConfig) -> dict[str, str]:
    suffix = _key(config)
    return {"maintain": f"Meta Memory Maintain {suffix}", "dream": f"Meta Memory Dream {suffix}"}


def _enabled_actions(config: AppConfig) -> list[str]:
    actions: list[str] = []
    if config.maintenance_enabled:
        actions.append("maintain")
    if config.dream_enabled:
        actions.append("dream")
    return actions


def _windows_task_command(launcher: Path, action: str) -> str:
    # Task Scheduler only receives a stable .cmd invocation; all Python/config
    # quoting lives in the generated launcher itself.
    return f'cmd.exe /d /c ""{launcher}" {action}"'


def _linux_block(config: AppConfig, launcher: Path) -> str:
    rows = [_BEGIN]
    command = shlex.quote(str(launcher))
    if config.maintenance_enabled:
        rows.append(f"*/{max(1, int(config.maintenance_interval_minutes))} * * * * {command} maintain")
    if config.dream_enabled:
        hour, minute = _dream_time(config)
        rows.append(f"{minute} {hour} * * * {command} dream")
    rows.append(_END)
    return "\n".join(rows)


def _dream_time(config: AppConfig) -> tuple[int, int]:
    try:
        hour, minute = (int(part) for part in str(config.dream_schedule).split(":", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("dream.schedule must be HH:MM.") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("dream.schedule must be HH:MM.")
    return hour, minute


def _managed_crontab(current: str, block: str) -> str:
    expression = re.compile(r"(?:^|\n)# meta-memory:begin\n.*?# meta-memory:end(?:\n|$)", re.S)
    cleaned = expression.sub("\n", current).strip("\n")
    return (cleaned + "\n\n" if cleaned else "") + block + "\n"


def _mac_plist(config: AppConfig, label: str, action: str, launcher: Path) -> dict[str, object]:
    data: dict[str, object] = {
        "Label": label,
        "ProgramArguments": [str(launcher), action],
        "StandardOutPath": str(scheduler_log_path(config)),
        "StandardErrorPath": str(scheduler_log_path(config)),
        "RunAtLoad": False,
    }
    if action == "maintain":
        data["StartInterval"] = max(60, int(config.maintenance_interval_minutes) * 60)
    else:
        hour, minute = _dream_time(config)
        data["StartCalendarInterval"] = {"Hour": hour, "Minute": minute}
    return data


def schedule_install(config: AppConfig) -> dict[str, object]:
    """Create the launcher then install only the actions enabled in config."""
    actions = _enabled_actions(config)
    if not actions:
        return {"status": "skipped", "reason": "both schedules are disabled"}
    launcher = write_scheduler_launcher(config)
    if os.name == "nt":
        results = []
        for action in actions:
            args = ["schtasks", "/Create", "/F", "/TN", _task_names(config)[action], "/SC"]
            if action == "maintain":
                args.extend(["MINUTE", "/MO", str(max(1, int(config.maintenance_interval_minutes)))])
            else:
                hour, minute = _dream_time(config)
                args.extend(["DAILY", "/ST", f"{hour:02d}:{minute:02d}"])
            args.extend(["/TR", _windows_task_command(launcher, action)])
            completed = subprocess.run(args, capture_output=True, text=True, check=False)
            results.append({"action": action, "returncode": completed.returncode, "stderr": completed.stderr.strip()})
        failed = [item for item in results if item["returncode"]]
        if failed:
            raise RuntimeError(f"Windows Task Scheduler rejected Meta Memory tasks: {failed}")
        return {"status": "ok", "platform": "windows", "launcher": str(launcher), "tasks": results}
    if sys.platform == "darwin":
        directory = Path.home() / "Library" / "LaunchAgents"
        directory.mkdir(parents=True, exist_ok=True)
        uid = str(os.getuid())
        files: list[str] = []
        for action in actions:
            label = f"com.meta-memory.{action}.{_key(config)}"
            path = directory / f"{label}.plist"
            with path.open("wb") as handle:
                plistlib.dump(_mac_plist(config, label, action, launcher), handle)
            # bootout may legitimately fail when this is the first install.
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True, text=True, check=False)
            completed = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], capture_output=True, text=True, check=False)
            if completed.returncode:
                raise RuntimeError(f"launchctl bootstrap failed for {label}: {completed.stderr.strip()}")
            subprocess.run(["launchctl", "enable", f"gui/{uid}/{label}"], capture_output=True, text=True, check=False)
            files.append(str(path))
        return {"status": "ok", "platform": "macos", "launcher": str(launcher), "files": files}
    current_run = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
    if current_run.returncode not in {0, 1}:
        raise RuntimeError(f"Could not read crontab: {current_run.stderr.strip()}")
    updated = _managed_crontab(current_run.stdout if current_run.returncode == 0 else "", _linux_block(config, launcher))
    completed = subprocess.run(["crontab", "-"], input=updated, capture_output=True, text=True, check=False)
    if completed.returncode:
        raise RuntimeError(f"Could not install crontab: {completed.stderr.strip()}")
    return {"status": "ok", "platform": "linux", "launcher": str(launcher), "tasks": actions}


def schedule_status(config: AppConfig) -> dict[str, object]:
    launcher = scheduler_launcher_path(config)
    expected = _enabled_actions(config)
    if os.name == "nt":
        rows = []
        for action in ("maintain", "dream"):
            completed = subprocess.run(["schtasks", "/Query", "/TN", _task_names(config)[action]], capture_output=True, text=True, check=False)
            rows.append({"action": action, "installed": completed.returncode == 0, "detail": (completed.stdout or completed.stderr).strip()})
        return {"status": "ok", "platform": "windows", "launcher": str(launcher), "launcher_exists": launcher.is_file(), "tasks": rows}
    if sys.platform == "darwin":
        uid = str(os.getuid()); rows = []
        for action in ("maintain", "dream"):
            label = f"com.meta-memory.{action}.{_key(config)}"
            completed = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"], capture_output=True, text=True, check=False)
            rows.append({"action": action, "installed": completed.returncode == 0})
        return {"status": "ok", "platform": "macos", "launcher": str(launcher), "launcher_exists": launcher.is_file(), "tasks": rows}
    completed = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
    text = completed.stdout if completed.returncode == 0 else ""
    return {"status": "ok", "platform": "linux", "launcher": str(launcher), "launcher_exists": launcher.is_file(), "managed_block": _BEGIN in text and _END in text, "expected": expected}


def schedule_remove(config: AppConfig) -> dict[str, object]:
    launcher = scheduler_launcher_path(config)
    if os.name == "nt":
        rows = []
        for action in ("maintain", "dream"):
            completed = subprocess.run(["schtasks", "/Delete", "/F", "/TN", _task_names(config)[action]], capture_output=True, text=True, check=False)
            rows.append({"action": action, "removed": completed.returncode == 0})
        return {"status": "ok", "platform": "windows", "tasks": rows}
    if sys.platform == "darwin":
        uid = str(os.getuid()); removed = []
        for action in ("maintain", "dream"):
            label = f"com.meta-memory.{action}.{_key(config)}"
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True, text=True, check=False)
            path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
            path.unlink(missing_ok=True); removed.append(str(path))
        return {"status": "ok", "platform": "macos", "files": removed}
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
    updated = _managed_crontab(current.stdout if current.returncode == 0 else "", "").strip()
    completed = subprocess.run(["crontab", "-"], input=(updated + "\n") if updated else "", capture_output=True, text=True, check=False)
    if completed.returncode:
        raise RuntimeError(f"Could not update crontab: {completed.stderr.strip()}")
    return {"status": "ok", "platform": "linux", "launcher": str(launcher)}


def schedule_run(config: AppConfig, action: str) -> dict[str, Any]:
    if action not in {"maintain", "dream"}:
        raise ValueError("Schedule action must be maintain or dream.")
    launcher = write_scheduler_launcher(config)
    command = [str(launcher), action] if os.name != "nt" else [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", f'"{launcher}" {action}']
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    return {"status": "ok" if completed.returncode == 0 else "error", "action": action, "returncode": completed.returncode, "stdout": completed.stdout.strip(), "stderr": completed.stderr.strip(), "launcher": str(launcher)}


# Compatibility name retained for callers from the original public CLI.
def install_schedule(config: AppConfig) -> dict[str, object]:
    return schedule_install(config)
