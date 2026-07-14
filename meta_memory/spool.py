"""Small durable retry spool for a completed response that could not be saved."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pending_dir(config: AppConfig) -> Path:
    return Path(config.path).expanduser().resolve().parent / "spool" / "pending"


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def spool_completion(config: AppConfig, *, turn_uid: str, assistant_text: str, agent_id: str = "", error: str = "") -> dict[str, object]:
    if not turn_uid.strip() or not assistant_text.strip():
        raise ValueError("A turn id and assistant response are required for a deferred completion.")
    payload = {
        "operation": "complete_turn",
        "turn_uid": turn_uid,
        "assistant_text": assistant_text,
        "agent_id": agent_id,
        "created_at": _now(),
        "updated_at": _now(),
        "attempt_count": 0,
        "last_error": error[:1000] if error else "",
    }
    path = pending_dir(config) / f"complete-turn-{turn_uid}-{uuid.uuid4().hex}.json"
    _write(path, payload)
    return {"status": "spooled", "path": str(path), "turn_id": turn_uid}


def replay_spool(config: AppConfig, *, limit: int = 100) -> dict[str, object]:
    """Replay idempotent turn completions before normal maintenance work."""
    root = pending_dir(config)
    replayed: list[dict[str, object]] = []
    if not root.is_dir():
        return {"status": "ok", "replayed": replayed, "pending": 0}
    from .turn_service import complete_turn

    files = sorted(root.glob("*.json"))[: max(1, limit)]
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("operation") != "complete_turn":
                raise ValueError("Unsupported spool operation")
            result = complete_turn(
                config,
                turn_uid=str(payload.get("turn_uid") or ""),
                assistant_text=str(payload.get("assistant_text") or ""),
                agent_id=str(payload.get("agent_id") or ""),
            )
            path.unlink(missing_ok=True)
            replayed.append({"path": str(path), "status": "completed", "result": result})
        except Exception as exc:  # Retain failure for the next maintenance run.
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                payload = {"operation": "unknown", "created_at": _now(), "attempt_count": 0}
            payload["attempt_count"] = int(payload.get("attempt_count") or 0) + 1
            payload["updated_at"] = _now()
            payload["last_error"] = str(exc)[:1000]
            _write(path, payload)
            replayed.append({"path": str(path), "status": "pending", "error": str(exc)})
    return {"status": "ok", "replayed": replayed, "pending": len(list(root.glob("*.json")))}
