"""Generate the single absolute-path launcher used by system schedulers."""
from __future__ import annotations

import os
import shlex
import stat
import sys
from pathlib import Path

from .config import AppConfig


def scheduler_log_path(config: AppConfig) -> Path:
    return Path(config.path).expanduser().resolve().parent / "logs" / "scheduler.log"


def scheduler_launcher_path(config: AppConfig, *, windows: bool | None = None) -> Path:
    is_windows = os.name == "nt" if windows is None else windows
    root = Path(config.path).expanduser().resolve().parent / "bin"
    return root / f"meta-memory-system{'.cmd' if is_windows else ''}"


def _quote_cmd(value: str | Path) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def write_scheduler_launcher(config: AppConfig, *, windows: bool | None = None, python_executable: str | Path | None = None) -> Path:
    is_windows = os.name == "nt" if windows is None else windows
    launcher = scheduler_launcher_path(config, windows=is_windows)
    log = scheduler_log_path(config)
    launcher.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    executable = Path(python_executable or sys.executable).expanduser().resolve()
    config_path = Path(config.path).expanduser().resolve()
    if is_windows:
        content = (
            "@echo off\r\n"
            "setlocal\r\n"
            f"{_quote_cmd(executable)} -m meta_memory.cli --config {_quote_cmd(config_path)} --agent-id system %* >> {_quote_cmd(log)} 2>&1\r\n"
            "exit /b %ERRORLEVEL%\r\n"
        )
    else:
        content = (
            "#!/bin/sh\n"
            f"exec {shlex.quote(str(executable))} -m meta_memory.cli --config {shlex.quote(str(config_path))} --agent-id system \"$@\" >> {shlex.quote(str(log))} 2>&1\n"
        )
    launcher.write_text(content, encoding="utf-8", newline="" if is_windows else "\n")
    if not is_windows:
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return launcher
