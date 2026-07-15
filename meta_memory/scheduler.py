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
from .scheduler_launcher import (
    record_schedule_plan,
    run_scheduled_action,
    scheduler_launcher_path,
    scheduler_log_path,
    scheduler_runtime_status,
    write_scheduler_launcher,
)


_BEGIN = "# meta-memory:begin"
_END = "# meta-memory:end"


def _key(config: AppConfig) -> str:
    return hashlib.sha256(str(Path(config.path).expanduser().resolve()).encode("utf-8")).hexdigest()[:10]


def _task_names(config: AppConfig) -> dict[str, str]:
    suffix = _key(config)
    return {"maintain": f"Meta Memory Dream Heartbeat {suffix}", "dream": f"Meta Memory Dream Deep {suffix}"}


def _enabled_actions(config: AppConfig) -> list[str]:
    actions: list[str] = []
    if bool(getattr(config, "dream_heartbeat_enabled", config.maintenance_enabled)):
        actions.append("maintain")
    if bool(getattr(config, "dream_deep_enabled", config.dream_enabled)):
        actions.append("dream")
    return actions


def _windows_task_command(launcher: Path, action: str) -> str:
    # Task Scheduler only receives a stable .cmd invocation; all Python/config
    # quoting lives in the generated launcher itself.
    return f'cmd.exe /d /c ""{launcher}" {action}"'


def _heartbeat_interval(config: AppConfig) -> int:
    return max(1, int(getattr(config, "dream_heartbeat_interval_minutes", config.maintenance_interval_minutes)))


def _linux_heartbeat_schedule(config: AppConfig) -> tuple[str, int, bool]:
    """Return cron expression, platform tick, and whether the launcher gates it.

    Cron's ``*/N`` is not valid for N>59 and is not a true interval across an
    hour for many values.  Exact calendar-friendly intervals get native cron;
    all other long cadences use a small tick plus the durable due gate in the
    launcher, so the configured interval remains authoritative.
    """

    interval = _heartbeat_interval(config)
    if interval <= 59:
        return f"*/{interval} * * * *", interval, True
    if interval == 60:
        return "0 * * * *", interval, True
    if interval < 1440 and interval % 60 == 0 and 24 % (interval // 60) == 0:
        return f"0 */{interval // 60} * * *", interval, True
    if interval == 1440:
        return "0 0 * * *", interval, True
    if interval == 10080:
        return "0 0 * * 0", interval, True
    # A 15-minute tick is intentionally bounded even for a one-week desired
    # cadence; scheduler_runtime_state makes it a no-op until it is due.
    tick = min(15, interval)
    return f"*/{tick} * * * *", tick, True


def _windows_heartbeat_schedule(config: AppConfig) -> tuple[list[str], int]:
    interval = _heartbeat_interval(config)
    if interval <= 59:
        return ["MINUTE", "/MO", str(interval)], interval
    if interval == 60:
        return ["HOURLY", "/MO", "1"], interval
    if interval < 1440 and interval % 60 == 0 and interval // 60 <= 23:
        return ["HOURLY", "/MO", str(interval // 60)], interval
    if interval % 1440 == 0 and interval // 1440 <= 365:
        return ["DAILY", "/MO", str(interval // 1440)], interval
    tick = min(15, interval)
    return ["MINUTE", "/MO", str(tick)], tick


def _linux_block(config: AppConfig, launcher: Path) -> str:
    rows = [_BEGIN]
    command = shlex.quote(str(launcher))
    if bool(getattr(config, "dream_heartbeat_enabled", config.maintenance_enabled)):
        expression, _tick, _guarded = _linux_heartbeat_schedule(config)
        rows.append(f"{expression} {command} maintain")
    if bool(getattr(config, "dream_deep_enabled", config.dream_enabled)):
        hour, minute = _dream_time(config)
        rows.append(f"{minute} {hour} * * * {command} dream")
    rows.append(_END)
    return "\n".join(rows)


def _dream_time(config: AppConfig) -> tuple[int, int]:
    try:
        hour, minute = (int(part) for part in str(getattr(config, "dream_deep_schedule", config.dream_schedule)).split(":", 1))
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
        data["StartInterval"] = max(60, int(getattr(config, "dream_heartbeat_interval_minutes", config.maintenance_interval_minutes)) * 60)
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
        _windows_args, heartbeat_tick = _windows_heartbeat_schedule(config)
        for action in actions:
            args = ["schtasks", "/Create", "/F", "/TN", _task_names(config)[action], "/SC"]
            if action == "maintain":
                args.extend(_windows_args)
            else:
                hour, minute = _dream_time(config)
                args.extend(["DAILY", "/ST", f"{hour:02d}:{minute:02d}"])
            args.extend(["/TR", _windows_task_command(launcher, action)])
            completed = subprocess.run(args, capture_output=True, text=True, check=False)
            results.append({"action": action, "returncode": completed.returncode, "stderr": completed.stderr.strip()})
        failed = [item for item in results if item["returncode"]]
        if failed:
            raise RuntimeError(f"Windows Task Scheduler rejected Meta Memory tasks: {failed}")
        for action in actions:
            record_schedule_plan(config, action=action, installed_interval_minutes=(heartbeat_tick if action == "maintain" else None))
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
            record_schedule_plan(config, action=action, installed_interval_minutes=(_heartbeat_interval(config) if action == "maintain" else None))
            files.append(str(path))
        return {"status": "ok", "platform": "macos", "launcher": str(launcher), "files": files}
    current_run = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
    if current_run.returncode not in {0, 1}:
        raise RuntimeError(f"Could not read crontab: {current_run.stderr.strip()}")
    updated = _managed_crontab(current_run.stdout if current_run.returncode == 0 else "", _linux_block(config, launcher))
    completed = subprocess.run(["crontab", "-"], input=updated, capture_output=True, text=True, check=False)
    if completed.returncode:
        raise RuntimeError(f"Could not install crontab: {completed.stderr.strip()}")
    _expression, tick, _guarded = _linux_heartbeat_schedule(config)
    for action in actions:
        record_schedule_plan(config, action=action, installed_interval_minutes=(tick if action == "maintain" else None))
    return {"status": "ok", "platform": "linux", "launcher": str(launcher), "tasks": actions}


def schedule_status(config: AppConfig) -> dict[str, object]:
    launcher = scheduler_launcher_path(config)
    expected = _enabled_actions(config)
    runtime = scheduler_runtime_status(config)
    log = scheduler_log_path(config)
    if os.name == "nt":
        rows = []
        for action in ("maintain", "dream"):
            completed = subprocess.run(["schtasks", "/Query", "/TN", _task_names(config)[action]], capture_output=True, text=True, check=False)
            rows.append({"action": action, "installed": completed.returncode == 0, "detail": (completed.stdout or completed.stderr).strip()})
        _args, tick = _windows_heartbeat_schedule(config)
        return {"status": "ok", "platform": "windows", "launcher": str(launcher), "launcher_exists": launcher.is_file(), "tasks": rows, "expected": expected, "runtime": runtime, "log_path": str(log), "log_exists": log.is_file(), "heartbeat_schedule": {"task_arguments": _args, "installed_tick_minutes": tick, "due_gate": True, "desired_interval_minutes": _heartbeat_interval(config)}}
    if sys.platform == "darwin":
        uid = str(os.getuid()); rows = []
        for action in ("maintain", "dream"):
            label = f"com.meta-memory.{action}.{_key(config)}"
            completed = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"], capture_output=True, text=True, check=False)
            rows.append({"action": action, "installed": completed.returncode == 0})
        return {"status": "ok", "platform": "macos", "launcher": str(launcher), "launcher_exists": launcher.is_file(), "tasks": rows, "expected": expected, "runtime": runtime, "log_path": str(log), "log_exists": log.is_file(), "heartbeat_schedule": {"start_interval_seconds": _heartbeat_interval(config) * 60, "installed_tick_minutes": _heartbeat_interval(config), "due_gate": True, "desired_interval_minutes": _heartbeat_interval(config)}}
    completed = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
    text = completed.stdout if completed.returncode == 0 else ""
    expression, tick, guarded = _linux_heartbeat_schedule(config)
    return {"status": "ok", "platform": "linux", "launcher": str(launcher), "launcher_exists": launcher.is_file(), "managed_block": _BEGIN in text and _END in text, "expected": expected, "runtime": runtime, "log_path": str(log), "log_exists": log.is_file(), "heartbeat_schedule": {"cron": expression, "installed_tick_minutes": tick, "due_gate": guarded, "desired_interval_minutes": _heartbeat_interval(config)}}


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
    result = run_scheduled_action(config, action=action, force=True)
    result["launcher"] = str(launcher)
    return result


# Compatibility name retained for callers from the original public CLI.
def install_schedule(config: AppConfig) -> dict[str, object]:
    return schedule_install(config)
