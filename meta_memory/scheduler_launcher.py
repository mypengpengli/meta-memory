"""Generate the single absolute-path launcher used by system schedulers."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig, load_config


def scheduler_log_path(config: AppConfig) -> Path:
    return Path(config.path).expanduser().resolve().parent / "logs" / "scheduler.log"


def _config_key(config: AppConfig) -> str:
    return hashlib.sha256(str(Path(config.path).expanduser().resolve()).encode("utf-8")).hexdigest()[:10]


def _heartbeat_interval(config: AppConfig) -> int:
    return max(1, int(getattr(config, "dream_heartbeat_interval_minutes", config.maintenance_interval_minutes)))


def _rotate_log(config: AppConfig) -> None:
    """Rotate the scheduler log before a scheduled subprocess writes to it."""

    log = scheduler_log_path(config)
    try:
        maximum = max(64 * 1024, int(getattr(config, "scheduler_log_max_bytes", 2 * 1024 * 1024)))
        keep = max(1, int(getattr(config, "scheduler_log_keep_files", 4)))
        if not log.is_file() or log.stat().st_size < maximum:
            return
        for index in range(keep, 0, -1):
            source = log.with_name(f"{log.name}.{index}")
            destination = log.with_name(f"{log.name}.{index + 1}")
            if index == keep:
                source.unlink(missing_ok=True)
            elif source.exists():
                source.replace(destination)
        log.replace(log.with_name(f"{log.name}.1"))
    except OSError:
        # Scheduling must not fail merely because an old log is open on the
        # host.  The normal append below remains useful observability.
        return


def _append_log(config: AppConfig, text: str) -> None:
    log = scheduler_log_path(config)
    log.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    try:
        with log.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"[{stamp}] {text.rstrip()}\n")
    except OSError:
        pass


def _parse_iso(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _next_deep_due(config: AppConfig, now: datetime) -> str:
    try:
        hour, minute = (int(part) for part in str(getattr(config, "dream_deep_schedule", config.dream_schedule)).split(":", 1))
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate.isoformat()
    except (TypeError, ValueError):
        return (now + timedelta(days=1)).isoformat()


def record_schedule_plan(config: AppConfig, *, action: str, installed_interval_minutes: int | None) -> None:
    """Persist expected vs platform tick cadence for status/overview."""

    from .legacy import bootstrap

    bootstrap()
    from _common import open_db

    desired = _heartbeat_interval(config) if action == "maintain" else None
    conn = open_db(Path(config.store))
    try:
        conn.execute(
            """
            INSERT INTO scheduler_runtime_state(
                config_key,action,desired_interval_minutes,installed_interval_minutes,
                last_log_path,updated_at
            ) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(config_key,action) DO UPDATE SET
                desired_interval_minutes=excluded.desired_interval_minutes,
                installed_interval_minutes=excluded.installed_interval_minutes,
                last_log_path=excluded.last_log_path,updated_at=CURRENT_TIMESTAMP
            """,
            (_config_key(config), action, desired, installed_interval_minutes, str(scheduler_log_path(config))),
        )
        conn.commit()
    finally:
        conn.close()


def scheduler_runtime_status(config: AppConfig) -> dict[str, object]:
    """Read local scheduler observability without calling platform schedulers."""

    from .legacy import bootstrap

    bootstrap()
    from _common import open_db

    conn = open_db(Path(config.store))
    try:
        rows = conn.execute(
            """
            SELECT action,desired_interval_minutes,installed_interval_minutes,
                   last_started_at,last_completed_at,next_due_at,last_status,
                   last_exit_code,last_error,last_log_path,updated_at
            FROM scheduler_runtime_state WHERE config_key=? ORDER BY action
            """,
            (_config_key(config),),
        ).fetchall()
    finally:
        conn.close()
    values = []
    for row in rows:
        values.append(
            {
                "action": str(row[0]),
                "desired_interval_minutes": int(row[1]) if row[1] is not None else None,
                "installed_interval_minutes": int(row[2]) if row[2] is not None else None,
                "last_started_at": str(row[3] or ""),
                "last_completed_at": str(row[4] or ""),
                "next_due_at": str(row[5] or ""),
                "last_status": str(row[6] or "never"),
                "last_exit_code": int(row[7]) if row[7] is not None else None,
                "last_error": str(row[8] or ""),
                "log_path": str(row[9] or scheduler_log_path(config)),
                "updated_at": str(row[10] or ""),
            }
        )
    return {"status": "ok", "log_path": str(scheduler_log_path(config)), "actions": values}


def _claim_due(config: AppConfig, *, action: str, force: bool) -> tuple[bool, str]:
    """Atomically decide whether a platform tick is due to run real work."""

    from .legacy import bootstrap

    bootstrap()
    from _common import open_db

    now = datetime.now(timezone.utc)
    desired = _heartbeat_interval(config) if action == "maintain" else None
    conn = open_db(Path(config.store))
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT next_due_at FROM scheduler_runtime_state WHERE config_key=? AND action=?",
            (_config_key(config), action),
        ).fetchone()
        existing_due = _parse_iso(row[0]) if row else None
        if action == "maintain" and not force and existing_due and existing_due > now:
            conn.commit()
            return False, existing_due.isoformat()
        next_due = (
            (now + timedelta(minutes=desired)).isoformat()
            if action == "maintain"
            else _next_deep_due(config, now)
        )
        conn.execute(
            """
            INSERT INTO scheduler_runtime_state(
                config_key,action,desired_interval_minutes,last_started_at,next_due_at,
                last_status,last_error,last_log_path,updated_at
            ) VALUES(?,?,?,?,?,'running',NULL,?,?)
            ON CONFLICT(config_key,action) DO UPDATE SET
                desired_interval_minutes=excluded.desired_interval_minutes,
                last_started_at=excluded.last_started_at,next_due_at=excluded.next_due_at,
                last_status='running',last_error=NULL,last_log_path=excluded.last_log_path,
                updated_at=excluded.updated_at
            """,
            (
                _config_key(config),
                action,
                desired,
                now.isoformat(),
                next_due,
                str(scheduler_log_path(config)),
                now.isoformat(),
            ),
        )
        conn.commit()
        return True, next_due
    finally:
        conn.close()


def _record_result(config: AppConfig, *, action: str, returncode: int, error: str = "") -> None:
    from .legacy import bootstrap

    bootstrap()
    from _common import open_db

    now = datetime.now(timezone.utc).isoformat()
    conn = open_db(Path(config.store))
    try:
        conn.execute(
            """
            UPDATE scheduler_runtime_state
            SET last_completed_at=?,last_status=?,last_exit_code=?,last_error=?,
                last_log_path=?,updated_at=?
            WHERE config_key=? AND action=?
            """,
            (
                now,
                "ok" if returncode == 0 else "failed",
                int(returncode),
                str(error or "")[:2000] or None,
                str(scheduler_log_path(config)),
                now,
                _config_key(config),
                action,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def run_scheduled_action(config: AppConfig, *, action: str, agent_id: str = "system", force: bool = False) -> dict[str, Any]:
    """Run a scheduled action through a due gate and a rotated durable log."""

    if action not in {"maintain", "dream"}:
        raise ValueError("Schedule action must be maintain or dream.")
    due, next_due = _claim_due(config, action=action, force=force)
    if not due:
        _append_log(config, f"{action}: skipped; next due {next_due}")
        return {"status": "skipped", "action": action, "reason": "not_due", "next_due_at": next_due}
    _rotate_log(config)
    command = [
        str(Path(sys.executable).resolve()),
        "-m",
        "meta_memory.cli",
        "--config",
        str(Path(config.path).expanduser().resolve()),
        "--agent-id",
        agent_id or "system",
        "maintain" if action == "maintain" else "dream",
    ]
    if action == "dream":
        command.append("deep")
    _append_log(config, f"{action}: started")
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    output = (completed.stdout or "").strip()
    error = (completed.stderr or "").strip()
    if output:
        _append_log(config, output)
    if error:
        _append_log(config, error)
    _record_result(config, action=action, returncode=completed.returncode, error=error)
    _append_log(config, f"{action}: {'ok' if completed.returncode == 0 else 'failed'} ({completed.returncode})")
    return {
        "status": "ok" if completed.returncode == 0 else "error",
        "action": action,
        "returncode": int(completed.returncode),
        "stdout": output,
        "stderr": error,
        "next_due_at": next_due,
        "log_path": str(scheduler_log_path(config)),
    }


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
            "REM Scheduled work is ultimately executed by -m meta_memory.cli.\r\n"
            f"{_quote_cmd(executable)} -m meta_memory.scheduler_launcher --config {_quote_cmd(config_path)} --agent-id system --action %*\r\n"
            "exit /b %ERRORLEVEL%\r\n"
        )
    else:
        content = (
            "#!/bin/sh\n"
            "# Scheduled work is ultimately executed by -m meta_memory.cli.\n"
            f"exec {shlex.quote(str(executable))} -m meta_memory.scheduler_launcher --config {shlex.quote(str(config_path))} --agent-id system --action \"$1\"\n"
        )
    launcher.write_text(content, encoding="utf-8", newline="" if is_windows else "\n")
    if not is_windows:
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return launcher


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one guarded Meta Memory scheduled action.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--agent-id", default="system")
    parser.add_argument("--action", required=True, choices=["maintain", "dream"])
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    result = run_scheduled_action(
        load_config(args.config), action=args.action, agent_id=args.agent_id, force=args.force
    )
    print(json.dumps(result, ensure_ascii=False, default=str))
    raise SystemExit(0 if result["status"] in {"ok", "skipped"} else int(result.get("returncode", 1) or 1))


if __name__ == "__main__":
    main()
