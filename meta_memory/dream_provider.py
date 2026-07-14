"""Optional, tightly constrained semantic Dream provider support."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Protocol


class DreamProvider(Protocol):
    def synthesize(self, payload: dict[str, object]) -> dict[str, object]: ...


def validate_output(payload: object) -> dict[str, list[str]]:
    if not isinstance(payload, dict):
        raise ValueError("Dream provider must return a JSON object.")
    result: dict[str, list[str]] = {}
    for key in ("project_digest", "patterns", "procedure_candidates", "open_questions"):
        value = payload.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) and len(item) <= 2000 for item in value):
            raise ValueError(f"Dream provider field {key} must be a list of bounded strings.")
        result[key] = value[:20]
    return result


def command_synthesize(command: str, payload: dict[str, object], *, timeout_seconds: int = 30) -> dict[str, list[str]]:
    if not command.strip():
        raise ValueError("dream.command is required when dream.provider='command'.")
    argv = shlex.split(command, posix=os.name != "nt")
    if os.name == "nt":
        argv = [item[1:-1] if len(item) >= 2 and item[:1] == item[-1:] == '"' else item for item in argv]
    if not argv:
        raise ValueError("dream.command is required when dream.provider='command'.")
    completed = subprocess.run(
        argv,
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(1, timeout_seconds),
        check=False,
        shell=False,
    )
    if completed.returncode:
        raise RuntimeError((completed.stderr or "Dream provider failed.")[:2000])
    if len(completed.stdout.encode("utf-8")) > 1_000_000:
        raise ValueError("Dream provider output is too large.")
    return validate_output(json.loads(completed.stdout))
