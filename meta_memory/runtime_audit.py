"""Privacy-preserving operational counters and error audit records."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .config import AppConfig
from .project_detection import ProjectContext, _git_remote_identity


_SECRET = re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|bearer\s+|(?:api[_-]?key|token|cookie|password|secret)\s*[:=]\s*)[^\s,;]+")


def repository_fingerprint(project: ProjectContext) -> str:
    # Project resolution has already paid for the Git lookup.  Keep the
    # fallback for third-party callers that still construct ProjectContext
    # directly using its original three positional fields.
    cached = str(getattr(project, "repository_fingerprint", "") or "")
    if cached:
        return cached
    remote = str(getattr(project, "remote_identity", "") or "") or _git_remote_identity(project.root)
    material = remote or str(Path(project.root).expanduser().resolve())
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _safe_error(value: object) -> str:
    text = _SECRET.sub(r"\1[redacted]", " ".join(str(value or "").split()))
    return text[:1000]


def project_identity_warnings(config: AppConfig, *, agent_id: str, project: ProjectContext) -> list[dict[str, str]]:
    """Warn about conflicting local bindings without modifying any identity."""

    from _common import open_db

    root = str(Path(project.root).expanduser().resolve())
    fingerprint = repository_fingerprint(project)
    conn = open_db(Path(config.store))
    try:
        rows = conn.execute(
            """
            SELECT agent_id,project_id,project_root,repository_fingerprint
            FROM agent_runtime_state WHERE profile_id=? AND agent_id!=?
              AND ((project_root=? AND COALESCE(project_id,'')!=?)
                   OR (repository_fingerprint=? AND COALESCE(project_id,'')!=?))
            ORDER BY updated_at DESC LIMIT 3
            """,
            (config.profile_id, agent_id, root, project.project_id, fingerprint, project.project_id),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "code": "project_identity_mismatch",
            "current_agent": agent_id,
            "current_project": project.project_id,
            "other_agent": str(row[0]),
            "other_project": str(row[1] or ""),
            "action": f"Run meta-memory project set {project.project_id} from the same repository root.",
        }
        for row in rows
    ]


def _upsert_state(
    config: AppConfig,
    *,
    agent_id: str,
    workspace_id: str,
    project: ProjectContext | None = None,
    session_id: str = "",
    turn_uid: str = "",
    retrieval_count: int = 0,
    retrieval_duration_ms: int = 0,
    phase: str,
    degraded: bool = False,
    error_code: str = "",
    error_message: str = "",
) -> None:
    from _common import open_db, utc_now

    project_id = project.project_id if project else workspace_id.removeprefix("project:")
    project_root = str(Path(project.root).expanduser().resolve()) if project else ""
    fingerprint = repository_fingerprint(project) if project else ""
    now = utc_now()
    conn = open_db(Path(config.store))
    try:
        # Seed a row first, then apply the event-specific update.  Keeping the
        # two operations explicit makes the audit path easy to extend and, more
        # importantly, avoids a fragile, positional mega-UPSERT.
        conn.execute(
            """
            INSERT INTO agent_runtime_state(
                profile_id,agent_id,workspace_id,client_type,client_id,project_id,project_root,repository_fingerprint,
                last_before_at,last_after_at,last_write_at,last_retrieval_at,last_turn_uid,last_session_id,
                last_retrieval_count,last_retrieval_duration_ms,total_before,total_after,total_degraded,
                last_error_at,last_error_code,last_error_message,updated_at
            ) VALUES(?, ?, ?, 'agent', ?, ?, ?, ?, NULL, NULL, NULL, NULL,
                     NULL, NULL, 0, 0, 0, 0, 0, NULL, NULL, NULL, ?)
            ON CONFLICT(profile_id,agent_id,workspace_id) DO NOTHING
            """,
            (
                config.profile_id, agent_id, workspace_id, agent_id, project_id, project_root, fingerprint, now,
            ),
        )
        assignments = ["client_type='agent'", "client_id=?", "updated_at=?"]
        values: list[object] = [agent_id, now]
        if project:
            assignments.extend(["project_id=?", "project_root=?", "repository_fingerprint=?"])
            values.extend([project_id, project_root, fingerprint])
        if session_id:
            assignments.append("last_session_id=?")
            values.append(session_id)
        if turn_uid:
            assignments.append("last_turn_uid=?")
            values.append(turn_uid)
        if phase == "before":
            assignments.extend([
                "last_before_at=?", "last_retrieval_at=?", "last_retrieval_count=?",
                "last_retrieval_duration_ms=?", "total_before=total_before+1",
            ])
            values.extend([now, now, retrieval_count, retrieval_duration_ms])
        elif phase == "after":
            assignments.extend(["last_after_at=?", "last_write_at=?", "total_after=total_after+1"])
            values.extend([now, now])
        elif phase == "write":
            assignments.append("last_write_at=?")
            values.append(now)
        if degraded:
            assignments.append("total_degraded=total_degraded+1")
        if error_code:
            assignments.extend(["last_error_at=?", "last_error_code=?", "last_error_message=?"])
            values.extend([now, error_code, _safe_error(error_message)])
        elif phase in {"before", "after", "write"}:
            assignments.extend(["last_error_at=NULL", "last_error_code=NULL", "last_error_message=NULL"])
        values.extend([config.profile_id, agent_id, workspace_id])
        conn.execute(
            "UPDATE agent_runtime_state SET " + ", ".join(assignments)
            + " WHERE profile_id=? AND agent_id=? AND workspace_id=?",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def record_before(
    config: AppConfig,
    *,
    agent_id: str,
    project: ProjectContext,
    session_id: str,
    turn_uid: str,
    retrieval_count: int,
    retrieval_duration_ms: int,
    degraded: bool = False,
    error_code: str = "",
    error_message: str = "",
) -> None:
    _upsert_state(
        config, agent_id=agent_id, workspace_id=project.workspace_id, project=project, session_id=session_id,
        turn_uid=turn_uid, retrieval_count=retrieval_count, retrieval_duration_ms=retrieval_duration_ms,
        phase="before", degraded=degraded, error_code=error_code, error_message=error_message,
    )
    if error_code:
        record_error(config, agent_id=agent_id, workspace_id=project.workspace_id, turn_uid=turn_uid, phase="before", error_code=error_code, error_message=error_message)


def record_after(config: AppConfig, *, agent_id: str, workspace_id: str, session_id: str, turn_uid: str, idempotent: bool = False) -> None:
    _upsert_state(
        config, agent_id=agent_id, workspace_id=workspace_id, session_id=session_id, turn_uid=turn_uid,
        phase="after" if not idempotent else "write", degraded=False,
    )


def record_write(config: AppConfig, *, agent_id: str, workspace_id: str, turn_uid: str = "") -> None:
    _upsert_state(config, agent_id=agent_id, workspace_id=workspace_id, turn_uid=turn_uid, phase="write")


def record_error(config: AppConfig, *, agent_id: str, workspace_id: str, turn_uid: str, phase: str, error_code: str, error_message: str) -> None:
    from _common import open_db, utc_now

    safe = _safe_error(error_message)
    conn = open_db(Path(config.store))
    try:
        conn.execute(
            "INSERT INTO runtime_error_log(profile_id,agent_id,workspace_id,turn_uid,phase,error_code,error_message,created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (config.profile_id, agent_id, workspace_id or None, turn_uid or None, phase, error_code, safe, utc_now()),
        )
        conn.commit()
    finally:
        conn.close()


def record_turn_error(config: AppConfig, *, agent_id: str, turn_uid: str, phase: str, error_code: str, error_message: str) -> None:
    from _common import open_db

    conn = open_db(Path(config.store))
    try:
        row = conn.execute("SELECT workspace_id FROM turns WHERE turn_uid=?", (turn_uid,)).fetchone()
    finally:
        conn.close()
    workspace = str(row[0] or "") if row else ""
    _upsert_state(config, agent_id=agent_id, workspace_id=workspace or "unknown", turn_uid=turn_uid, phase="error", degraded=True, error_code=error_code, error_message=error_message)
    record_error(config, agent_id=agent_id, workspace_id=workspace, turn_uid=turn_uid, phase=phase, error_code=error_code, error_message=error_message)


def cleanup_error_log(config: AppConfig, *, retention_days: int = 30) -> int:
    from datetime import datetime, timedelta, timezone
    from _common import open_db

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))).isoformat()
    conn = open_db(Path(config.store))
    try:
        count = conn.execute("DELETE FROM runtime_error_log WHERE profile_id=? AND created_at<?", (config.profile_id, cutoff)).rowcount
        conn.commit()
        return int(count)
    finally:
        conn.close()
