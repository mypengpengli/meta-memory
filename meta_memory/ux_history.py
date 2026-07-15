"""Convenient public browsing around the durable session archive."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import AppConfig
from .legacy import bootstrap
from .project_detection import resolve_project
from .runtime import history as search_history
from .runtime import origin_agent_id


def _ready(config: AppConfig):
    bootstrap()
    from _common import ensure_store_ready, open_db

    root = Path(config.store)
    ensure_store_ready(root)
    return root, open_db(root)


def _agent_filter(config: AppConfig, agent_id: str) -> str:
    return origin_agent_id(agent_id) if str(getattr(config, "history_scope", "workspace-summary")) == "agent" else ""


def history_recent(
    config: AppConfig,
    *,
    project_name: str = "auto",
    start: str | Path | None = None,
    limit: int = 20,
    agent_id: str = "",
) -> dict[str, Any]:
    bootstrap()
    from session_search import browse

    project = resolve_project(config, project_name, start)
    result = browse(
        Path(config.store), subject_id=config.subject_id, recent=max(1, min(int(limit), 100)),
        workspace_id=project.workspace_id, profile_id=config.profile_id, agent_id=_agent_filter(config, agent_id),
    )
    return {"status": "ok", "project": project.project_id, "history_scope": config.history_scope, **result}


def history_search(
    config: AppConfig,
    *,
    query: str,
    project_name: str = "auto",
    start: str | Path | None = None,
    agent_id: str = "",
    detail: bool = False,
) -> dict[str, Any]:
    if not str(query or "").strip():
        raise ValueError("History search requires a query.")
    return search_history(
        config, query=query, project_name=project_name, start=start,
        agent_id=agent_id, detail=detail,
    )


def history_show(
    config: AppConfig,
    *,
    session_id: str,
    project_name: str = "auto",
    start: str | Path | None = None,
    agent_id: str = "",
    last: int = 8,
) -> dict[str, Any]:
    """Show a bounded completed-session detail, with an archive fallback."""
    project = resolve_project(config, project_name, start)
    reader_agent = _agent_filter(config, agent_id)
    root, conn = _ready(config)
    try:
        card_sql = (
            "SELECT id,session_id,origin_agent_id,summary,tool_summary,open_questions,completed_turn_count,last_completed_turn_at,updated_at "
            "FROM session_cards WHERE profile_id=? AND workspace_id=? AND subject_id=? AND session_id=?"
        )
        params: list[object] = [config.profile_id, project.workspace_id, config.subject_id, session_id]
        if reader_agent:
            card_sql += " AND COALESCE(origin_agent_id,'')=?"
            params.append(reader_agent)
        card = conn.execute(card_sql + " ORDER BY updated_at DESC LIMIT 1", params).fetchone()
        if card:
            summary = {
                "card_id": int(card[0]), "session_id": str(card[1]), "external_session_id": str(card[1]),
                "origin_agent_id": str(card[2] or ""), "summary": str(card[3] or ""),
                "tool_summary": str(card[4] or ""), "open_questions": [],
                "completed_turns": int(card[6] or 0), "last_completed_turn_at": str(card[7] or ""),
                "updated_at": str(card[8] or ""),
            }
            from session_search import read_session_detail

            detail = read_session_detail(
                root, summaries=[summary], subject_id=config.subject_id, workspace_id=project.workspace_id,
                profile_id=config.profile_id, max_sessions=1, max_turns=max(1, min(int(last), 50)),
                max_chars=int(getattr(config, "history_detail_max_chars", 12000)),
                tool_summary_max_chars=int(getattr(config, "history_tool_summary_max_chars", 1200)),
            )
            return {
                "status": "ok", "project": project.project_id, "mode": "detail", "session": summary,
                "details": detail,
            }

        # An active/unprocessed session may not have a completed Card yet.  It
        # is still useful to let its owner inspect the most recent user/final
        # messages rather than reporting an opaque not-found result.
        session_sql = (
            "SELECT session_id,external_session_id,origin_agent_id,source,title,status,started_at,last_active_at "
            "FROM sessions WHERE profile_id=? AND workspace_id=? AND subject_id=? "
            "AND (session_id=? OR external_session_id=?)"
        )
        params = [config.profile_id, project.workspace_id, config.subject_id, session_id, session_id]
        if reader_agent:
            session_sql += " AND COALESCE(origin_agent_id,'')=?"
            params.append(reader_agent)
        session = conn.execute(session_sql + " ORDER BY last_active_at DESC LIMIT 1", params).fetchone()
        if not session:
            raise ValueError("Session was not found in the selected project scope.")
        internal_id = str(session[0])
        rows = conn.execute(
            "SELECT role,content,timestamp FROM session_messages WHERE session_id=? AND role IN ('user','assistant') ORDER BY id DESC LIMIT ?",
            (internal_id, max(1, min(int(last), 100)) * 2),
        ).fetchall()
        messages = [
            {"role": str(row[0]), "content": str(row[1]), "timestamp": str(row[2] or "")}
            for row in reversed(rows)
        ]
    finally:
        conn.close()
    return {
        "status": "ok", "project": project.project_id, "mode": "archive", "session": {
            "internal_session_id": internal_id, "external_session_id": str(session[1] or ""),
            "origin_agent_id": str(session[2] or ""), "source": str(session[3] or ""),
            "title": str(session[4] or ""), "status": str(session[5] or ""),
            "started_at": str(session[6] or ""), "last_active_at": str(session[7] or ""),
        }, "messages": messages,
    }
