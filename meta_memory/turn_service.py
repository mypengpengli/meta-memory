"""Durable public turn lifecycle for the local shared-memory runtime."""
from __future__ import annotations

import argparse
import uuid
from pathlib import Path
from typing import Any

from .config import AppConfig
from .legacy import bootstrap
from .project_detection import ProjectContext, resolve_project


def _hash(text: str) -> str:
    bootstrap()
    from _common import sha256_text

    return sha256_text(text)


def _agent(value: str) -> str:
    return value.strip() or "generic-agent"


def _session(value: str) -> str:
    session_id = value.strip()
    if not session_id or session_id.casefold() == "auto":
        raise ValueError("A concrete session id is required until automatic session resolution is enabled.")
    return session_id


def _identity_matches(
    row,
    *,
    profile_id: str,
    workspace_id: str,
    subject_id: str,
    agent_id: str,
    session_id: str,
    request_hash: str,
) -> bool:
    return (
        str(row[0]) == profile_id
        and str(row[1]) == workspace_id
        and str(row[2]) == subject_id
        and str(row[3]) == agent_id
        and str(row[4]) == session_id
        and str(row[5] or "") == request_hash
    )


def _context_args(
    config: AppConfig,
    project: ProjectContext,
    *,
    session_id: str,
    agent_id: str,
    query: str,
) -> argparse.Namespace:
    return argparse.Namespace(
        store=str(config.store),
        subject_id=config.subject_id,
        subject_name="",
        session_id=session_id,
        profile_id=config.profile_id,
        workspace_id=project.workspace_id,
        agent_id=agent_id,
        visibility_scope="workspace",
        shared_mode=True,
        query=query,
        query_file=None,
        topic_hint="",
        domain_hint="",
        source_ref="",
        event_time="",
        skip_record_query=True,
        allow_duplicate=False,
        skip_heartbeat=True,
        heartbeat_policy="conservative",
        heartbeat_interval_minutes=config.maintenance_interval_minutes,
        heartbeat_min_pending=3,
        heartbeat_max_events=20,
        top_k=config.top_k,
        candidate_pool=max(config.top_k * 4, 24),
        expand_hops=1,
        include_candidates=False,
        no_basics=False,
        raw_limit=3,
        skip_raw_evidence=False,
        context_token_budget=1800,
        context_out_file=None,
        out_file=None,
        hot_snapshot_policy="frozen",
        include_embeddings=config.embeddings,
    )


def _update_context_status(root: Path, turn_uid: str, *, status: str, error: str = "") -> None:
    bootstrap()
    from _common import open_db, utc_now

    conn = open_db(root)
    try:
        conn.execute(
            "UPDATE turns SET context_status=?, last_error=?, updated_at=? WHERE turn_uid=?",
            (status, error or None, utc_now(), turn_uid),
        )
        conn.commit()
    finally:
        conn.close()


def begin_turn(
    config: AppConfig,
    *,
    query: str,
    project_name: str = "auto",
    requested_session: str,
    agent_id: str,
    cwd: str | Path | None = None,
    requested_turn_uid: str = "",
) -> dict[str, Any]:
    """Persist the user request before attempting retrieval.

    Retrieval is intentionally outside the creation transaction.  If it fails,
    the started turn and its pending user evidence remain durable and the host
    Agent can still answer without recalled context.
    """
    if not query.strip():
        raise ValueError("A request is required via --query or --query-file.")
    bootstrap()
    from _common import open_db, utc_now
    from ingest_raw_event import insert_raw_event_with_conn
    from memory_runtime import prepare_context
    from session_archive import ensure_session

    project = resolve_project(config, project_name, cwd)
    session_id = _session(requested_session)
    stable_agent = _agent(agent_id)
    turn_uid = requested_turn_uid.strip() or str(uuid.uuid4())
    request_hash = _hash(query)
    root = Path(config.store)

    conn = open_db(root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT profile_id,workspace_id,subject_id,origin_agent_id,external_session_id,request_hash,"
            "internal_session_id,user_event_id,assistant_event_id,review_job_uid,status,context_status "
            "FROM turns WHERE turn_uid=?",
            (turn_uid,),
        ).fetchone()
        created = not bool(existing)
        if existing:
            if not _identity_matches(
                existing,
                profile_id=config.profile_id,
                workspace_id=project.workspace_id,
                subject_id=config.subject_id,
                agent_id=stable_agent,
                session_id=session_id,
                request_hash=request_hash,
            ):
                raise ValueError("turn id is already bound to a different request or identity.")
            internal_session_id = str(existing[6] or "")
            user_event_id = int(existing[7] or 0)
            if not internal_session_id or not user_event_id:
                raise ValueError("Turn is incomplete and cannot safely be retried.")
        else:
            internal_session_id = ensure_session(
                root,
                subject_id=config.subject_id,
                session_id=session_id,
                profile_id=config.profile_id,
                workspace_id=project.workspace_id,
                shared_mode=True,
                conn=conn,
            )
            conn.execute(
                """
                INSERT INTO turns(
                    turn_uid,profile_id,workspace_id,subject_id,origin_agent_id,
                    external_session_id,internal_session_id,request_hash,status,
                    context_status,started_at,updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'started', 'pending', ?, ?)
                """,
                (
                    turn_uid,
                    config.profile_id,
                    project.workspace_id,
                    config.subject_id,
                    stable_agent,
                    session_id,
                    internal_session_id,
                    request_hash,
                    utc_now(),
                    utc_now(),
                ),
            )
            event = insert_raw_event_with_conn(
                conn,
                root,
                subject_id=config.subject_id,
                subject_name=config.user_name,
                session_id=session_id,
                source_type="conversation-user",
                source_ref="",
                content=query,
                profile_id=config.profile_id,
                workspace_id=project.workspace_id,
                origin_agent_id=stable_agent,
                visibility_scope="workspace",
                event_uid=f"turn:{turn_uid}:user",
                idempotency_key=f"turn:{turn_uid}:user",
                turn_uid=turn_uid,
                message_role="user",
                message_sequence=0,
                shared_mode=True,
            )
            user_event_id = int(event["raw_event_id"])
            conn.execute(
                "UPDATE turns SET user_event_id=?,updated_at=? WHERE turn_uid=?",
                (user_event_id, utc_now(), turn_uid),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    try:
        prepared = prepare_context(
            _context_args(config, project, session_id=session_id, agent_id=stable_agent, query=query)
        )
    except Exception as exc:
        _update_context_status(root, turn_uid, status="degraded", error=str(exc))
        return {
            "status": "degraded",
            "turn_id": turn_uid,
            "session_id": session_id,
            "session_source": "explicit",
            "project": project.project_id,
            "project_root": str(project.root),
            "agent_id": stable_agent,
            "hot_context": "",
            "context": "",
            "query_route": {},
            "warnings": ["Memory retrieval failed; continue without recalled memory."],
            "idempotent": not created,
            "user_event_id": user_event_id,
            "internal_session_id": internal_session_id,
        }

    _update_context_status(root, turn_uid, status="ready")
    return {
        "status": "ok",
        "turn_id": turn_uid,
        "session_id": session_id,
        "session_source": "explicit",
        "project": project.project_id,
        "project_root": str(project.root),
        "agent_id": stable_agent,
        "hot_context": prepared["static_hot_context"],
        "context": prepared["context_markdown"],
        "query_route": prepared["query_route"],
        "hot_memory_snapshot_hash": prepared["hot_memory_snapshot_hash"],
        "warnings": [],
        "idempotent": not created,
        "user_event_id": user_event_id,
        "internal_session_id": internal_session_id,
    }


def complete_turn(config: AppConfig, *, turn_uid: str, assistant_text: str) -> dict[str, Any]:
    """Atomically save the assistant response and enqueue downstream work."""
    if not turn_uid.strip():
        raise ValueError("A turn id is required.")
    if not assistant_text.strip():
        raise ValueError("Assistant content is required via --assistant or --assistant-file.")
    bootstrap()
    from _common import open_db, utc_now
    from background_review import enqueue_review
    from ingest_raw_event import insert_raw_event_with_conn

    root = Path(config.store)
    response_hash = _hash(assistant_text)
    conn = open_db(root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT turn_uid,profile_id,workspace_id,subject_id,origin_agent_id,
                   external_session_id,user_event_id,assistant_event_id,
                   review_job_uid,status,response_hash
            FROM turns WHERE turn_uid=?
            """,
            (turn_uid,),
        ).fetchone()
        if not row:
            raise ValueError("Turn not found.")
        if str(row[9]) == "abandoned":
            raise ValueError("Cannot complete an abandoned turn.")
        if str(row[9]) == "completed":
            if str(row[10] or "") != response_hash:
                raise ValueError("Turn is already completed with a different assistant response.")
            conn.commit()
            return {
                "status": "ok",
                "turn_id": str(row[0]),
                "user_event_id": int(row[6] or 0),
                "assistant_event_id": int(row[7] or 0),
                "review_job_id": str(row[8] or ""),
                "queued": bool(row[8]),
                "idempotent": True,
            }
        if not row[6]:
            raise ValueError("Turn has no persisted user event and cannot be completed.")

        user = conn.execute("SELECT subject_name FROM raw_events WHERE id=?", (int(row[6]),)).fetchone()
        event = insert_raw_event_with_conn(
            conn,
            root,
            subject_id=str(row[3]),
            subject_name=str(user[0] if user else config.user_name),
            session_id=str(row[5]),
            source_type="conversation-assistant",
            source_ref="",
            content=assistant_text,
            profile_id=str(row[1]),
            workspace_id=str(row[2]),
            origin_agent_id=str(row[4]),
            visibility_scope="workspace",
            event_uid=f"turn:{turn_uid}:assistant",
            idempotency_key=f"turn:{turn_uid}:assistant",
            turn_uid=turn_uid,
            message_role="assistant",
            message_sequence=1,
            shared_mode=True,
        )
        assistant_event_id = int(event["raw_event_id"])
        review = enqueue_review(
            root,
            subject_id=str(row[3]),
            session_id=str(row[5]),
            event_start_id=int(row[6]),
            event_end_id=assistant_event_id,
            trigger_type="turn_end",
            profile_id=str(row[1]),
            workspace_id=str(row[2]),
            origin_agent_id=str(row[4]),
            conn=conn,
        )
        conn.execute(
            """
            UPDATE turns
            SET assistant_event_id=?,review_job_uid=?,response_hash=?,status='completed',
                completed_at=?,updated_at=?,last_error=NULL
            WHERE turn_uid=?
            """,
            (assistant_event_id, str(review["job_id"]), response_hash, utc_now(), utc_now(), turn_uid),
        )
        conn.commit()
        return {
            "status": "ok",
            "turn_id": turn_uid,
            "user_event_id": int(row[6]),
            "assistant_event_id": assistant_event_id,
            "review_job_id": str(review["job_id"]),
            "queued": True,
            "idempotent": False,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def abandon_turn(config: AppConfig, *, turn_uid: str, reason: str = "") -> dict[str, Any]:
    """Mark an unfinished turn abandoned while preserving its user evidence."""
    if not turn_uid.strip():
        raise ValueError("A turn id is required.")
    bootstrap()
    from _common import open_db, utc_now

    conn = open_db(Path(config.store))
    try:
        changed = conn.execute(
            "UPDATE turns SET status='abandoned',last_error=?,updated_at=? "
            "WHERE turn_uid=? AND status='started'",
            (reason or "abandoned", utc_now(), turn_uid),
        ).rowcount
        conn.commit()
        return {"status": "ok", "turn_id": turn_uid, "abandoned": bool(changed)}
    finally:
        conn.close()
