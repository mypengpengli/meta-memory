"""Best-effort, explicit installation of one maintenance timer and one Dream timer."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import AppConfig


def _command(config: AppConfig, action: str) -> str:
    executable = shutil.which("meta-memory") or f'"{sys.executable}" -m meta_memory.cli'
    return f'{executable} --config "{config.path}" {action}'


def install_schedule(config: AppConfig) -> dict[str, object]:
    """Install platform-native tasks when the user selected them in setup."""
    if not config.maintenance_enabled and not config.dream_enabled:
        return {"status": "skipped", "reason": "both schedules are disabled"}
    if os.name == "nt":
        results = []
        if config.maintenance_enabled:
            results.append(subprocess.run(["schtasks", "/Create", "/F", "/TN", "Meta Memory Maintain", "/SC", "MINUTE", "/MO", str(config.maintenance_interval_minutes), "/TR", _command(config, "maintain")], capture_output=True, text=True).returncode)
        if config.dream_enabled:
            hour, minute = config.dream_schedule.split(":", 1)
            results.append(subprocess.run(["schtasks", "/Create", "/F", "/TN", "Meta Memory Dream", "/SC", "DAILY", "/ST", f"{int(hour):02d}:{int(minute):02d}", "/TR", _command(config, "dream")], capture_output=True, text=True).returncode)
        if any(code != 0 for code in results):
            raise RuntimeError("Windows Task Scheduler rejected one or more Meta Memory tasks.")
        return {"status": "ok", "platform": "windows", "tasks": len(results)}
    if sys.platform == "darwin":
        directory = Path.home() / "Library" / "LaunchAgents"
        directory.mkdir(parents=True, exist_ok=True)
        created = []
        if config.maintenance_enabled:
            path = directory / "com.meta-memory.maintain.plist"
            path.write_text(_plist("com.meta-memory.maintain", _command(config, "maintain"), interval=config.maintenance_interval_minutes * 60), encoding="utf-8")
            created.append(str(path))
        if config.dream_enabled:
            hour, minute = (int(value) for value in config.dream_schedule.split(":", 1))
            path = directory / "com.meta-memory.dream.plist"
            path.write_text(_plist("com.meta-memory.dream", _command(config, "dream"), calendar=(hour, minute)), encoding="utf-8")
            created.append(str(path))
        return {"status": "ok", "platform": "macos", "files": created, "note": "Run launchctl load for newly created files if launchd does not load them automatically."}
    marker = "# meta-memory managed schedule"
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    lines = [line for line in current.splitlines() if marker not in line and "meta-memory" not in line]
    if config.maintenance_enabled:
        lines.append(f"*/{config.maintenance_interval_minutes} * * * * {_command(config, 'maintain')} {marker}")
    if config.dream_enabled:
        hour, minute = config.dream_schedule.split(":", 1)
        lines.append(f"{int(minute)} {int(hour)} * * * {_command(config, 'dream')} {marker}")
    completed = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(f"Could not install crontab: {completed.stderr.strip()}")
    return {"status": "ok", "platform": "linux", "tasks": int(config.maintenance_enabled) + int(config.dream_enabled)}


def _plist(label: str, command: str, *, interval: int | None = None, calendar: tuple[int, int] | None = None) -> str:
    schedule = f"<key>StartInterval</key><integer>{interval}</integer>" if interval else f"<key>StartCalendarInterval</key><dict><key>Hour</key><integer>{calendar[0]}</integer><key>Minute</key><integer>{calendar[1]}</integer></dict>"
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>{label}</string><key>ProgramArguments</key><array><string>/bin/sh</string><string>-lc</string><string>{command}</string></array>{schedule}</dict></plist>
'''
