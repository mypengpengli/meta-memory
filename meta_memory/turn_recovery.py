"""Non-blocking detection and safe recovery of missed public ``after`` calls."""
from __future__ import annotations

from pathlib import Path

from .config import AppConfig


def _spool_available(config: AppConfig, turn_uid: str) -> bool:
    from .spool import pending_dir

    root = pending_dir(config)
    return root.is_dir() and any(root.glob(f"*{turn_uid}*.json"))


def unfinished_warnings(config: AppConfig, *, agent_id: str, workspace_id: str, exclude_turn_uid: str = "") -> list[dict[str, object]]:
    """Return aged same-Agent turns without interrupting the current answer."""

    from _common import open_db

    conn = open_db(Path(config.store))
    try:
        rows = conn.execute(
            """
            SELECT turn_uid,started_at FROM turns
            WHERE profile_id=? AND origin_agent_id=? AND workspace_id=? AND status='started'
              AND turn_uid!=? AND julianday(started_at)<=julianday('now', ?)
            ORDER BY started_at LIMIT 5
            """,
            (config.profile_id, agent_id, workspace_id, exclude_turn_uid, f"-{max(1, int(config.turns_unfinished_warning_minutes))} minutes"),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"code": "unfinished_previous_turn", "turn_id": str(row[0]), "started_at": str(row[1] or ""), "spool_available": _spool_available(config, str(row[0]))}
        for row in rows
    ]


def recover_expired_turns(config: AppConfig, *, limit: int = 50) -> dict[str, object]:
    """Replayable completions win; otherwise preserve user evidence and abandon."""

    from _common import open_db
    from .turn_service import abandon_turn, complete_turn

    conn = open_db(Path(config.store))
    try:
        rows = conn.execute(
            """
            SELECT turn_uid,origin_agent_id,assistant_event_id FROM turns
            WHERE profile_id=? AND status='started'
              AND julianday(started_at)<=julianday('now', ?)
            ORDER BY started_at LIMIT ?
            """,
            (config.profile_id, f"-{max(1, int(config.turns_abandon_after_minutes))} minutes", max(1, limit)),
        ).fetchall()
        assistant_text = {
            str(row[0]): str(
                (conn.execute("SELECT content FROM raw_events WHERE id=?", (int(row[2]),)).fetchone() or [""])[0] or ""
            )
            for row in rows if row[2]
        }
    finally:
        conn.close()
    recovered: list[str] = []
    abandoned: list[str] = []
    deferred: list[str] = []
    for turn_uid, agent_id, assistant_event_id in rows:
        uid = str(turn_uid)
        if _spool_available(config, uid):
            deferred.append(uid)
            continue
        text = assistant_text.get(uid, "") if assistant_event_id else ""
        if text:
            try:
                complete_turn(config, turn_uid=uid, assistant_text=text, agent_id=str(agent_id or ""))
                recovered.append(uid)
                continue
            except (ValueError, OSError, RuntimeError):
                deferred.append(uid)
                continue
        result = abandon_turn(config, turn_uid=uid, reason="after_not_received")
        if result.get("abandoned"):
            abandoned.append(uid)
    return {"status": "ok", "recovered": recovered, "abandoned": abandoned, "deferred": deferred}
