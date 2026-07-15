"""Public review inbox built on the existing durable proposal queues."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import AppConfig
from .legacy import bootstrap
from .project_detection import resolve_project
from .runtime import origin_agent_id


_STATUSES = {"pending", "applying", "approved", "rejected", "failed", "needs_clarification"}


def _ready(config: AppConfig):
    bootstrap()
    from _common import ensure_store_ready, open_db

    root = Path(config.store)
    ensure_store_ready(root)
    return root, open_db(root)


def _table(kind: str) -> str:
    value = str(kind or "memory").casefold()
    if value == "memory":
        return "write_proposals"
    if value == "skill":
        return "skill_proposals"
    raise ValueError("Inbox kind must be memory or skill.")


def inbox_list(
    config: AppConfig,
    *,
    project_name: str = "auto",
    start: str | Path | None = None,
    status: str = "pending",
    kind: str = "memory",
    limit: int = 50,
    all_projects: bool = False,
) -> dict[str, Any]:
    requested_kind = str(kind or "memory").casefold()
    statuses = [item.strip().casefold() for item in str(status or "pending").split(",") if item.strip()]
    if any(item not in _STATUSES and item != "all" for item in statuses):
        raise ValueError("Unsupported inbox status.")
    project = resolve_project(config, project_name, start)
    _, conn = _ready(config)
    try:
        kinds = ["memory", "skill"] if requested_kind == "all" else [requested_kind]
        rows: list[dict[str, Any]] = []
        for current in kinds:
            table = _table(current)
            if current == "memory":
                clauses = ["subject_id=?", "profile_id=?"]
                params: list[object] = [config.subject_id, config.profile_id]
                if not all_projects:
                    clauses.append("workspace_id=?")
                    params.append(project.workspace_id)
                if "all" not in statuses:
                    marks = ", ".join("?" for _ in statuses)
                    clauses.append(f"status IN ({marks})")
                    params.extend(statuses)
                sql = (
                    "SELECT proposal_uid,origin,action,summary,diff_text,status,created_at,reviewed_at,review_note,workspace_id "
                    f"FROM {table} WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?"
                )
                values = conn.execute(sql, (*params, max(1, min(int(limit), 500)))).fetchall()
                rows.extend(
                    {
                        "id": str(row[0]), "kind": current, "origin": str(row[1]), "action": str(row[2]),
                        "summary": str(row[3]), "diff": str(row[4] or ""), "status": str(row[5]),
                        "created_at": str(row[6]), "reviewed_at": str(row[7] or ""),
                        "review_note": str(row[8] or ""), "workspace_id": str(row[9] or ""),
                    }
                    for row in values
                )
            else:
                clauses = ["subject_id=?"]
                params = [config.subject_id]
                if "all" not in statuses:
                    marks = ", ".join("?" for _ in statuses)
                    clauses.append(f"status IN ({marks})")
                    params.extend(statuses)
                sql = (
                    "SELECT proposal_uid,origin,action,summary,diff_text,status,created_at,reviewed_at,review_note "
                    f"FROM {table} WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?"
                )
                values = conn.execute(sql, (*params, max(1, min(int(limit), 500)))).fetchall()
                rows.extend(
                    {
                        "id": str(row[0]), "kind": current, "origin": str(row[1]), "action": str(row[2]),
                        "summary": str(row[3]), "diff": str(row[4] or ""), "status": str(row[5]),
                        "created_at": str(row[6]), "reviewed_at": str(row[7] or ""),
                        "review_note": str(row[8] or ""), "workspace_id": None,
                    }
                    for row in values
                )
        rows.sort(key=lambda item: str(item["created_at"]), reverse=True)
    finally:
        conn.close()
    return {"status": "ok", "project": project.project_id, "inbox": rows[: max(1, min(int(limit), 500))], "returned": min(len(rows), max(1, min(int(limit), 500)))}


def inbox_show(config: AppConfig, *, proposal_id: str, kind: str = "memory") -> dict[str, Any]:
    bootstrap()
    from proposal_manager import get_proposal

    root, conn = _ready(config)
    conn.close()
    proposal = get_proposal(root, proposal_id, kind=str(kind or "memory").casefold())
    if not proposal:
        raise ValueError("Inbox proposal was not found.")
    return {"status": "ok", "kind": str(kind or "memory").casefold(), "proposal": proposal}


def inbox_approve(
    config: AppConfig,
    *,
    proposal_id: str,
    kind: str = "memory",
    agent_id: str = "",
) -> dict[str, Any]:
    current = str(kind or "memory").casefold()
    root, conn = _ready(config)
    conn.close()
    if current == "memory":
        from proposal_manager import approve_memory_proposal

        result = approve_memory_proposal(root, proposal_id)
        return {"kind": current, "proposal_id": proposal_id, **result}
    if current != "skill":
        raise ValueError("Inbox kind must be memory or skill.")
    # Skill proposals intentionally have no blind writer: a generated change
    # may target a host-owned file.  Approval marks the reviewed decision and
    # leaves the actual host update to the explicit Agent sync/repair command.
    _, conn = _ready(config)
    try:
        changed = conn.execute(
            "UPDATE skill_proposals SET status='approved',reviewed_at=CURRENT_TIMESTAMP,reviewed_by=?,review_note=? WHERE proposal_uid=? AND status='pending'",
            (origin_agent_id(agent_id), "Approved; apply through explicit Agent sync/repair.", proposal_id),
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    return {
        "status": "approved" if changed else "not_pending", "kind": current, "proposal_id": proposal_id,
        "manual_application_required": True,
    }


def inbox_reject(
    config: AppConfig,
    *,
    proposal_id: str,
    kind: str = "memory",
    note: str = "",
) -> dict[str, Any]:
    bootstrap()
    from proposal_manager import reject_proposal

    root, conn = _ready(config)
    conn.close()
    changed = reject_proposal(root, proposal_id, note=note, kind=str(kind or "memory").casefold())
    return {"status": "rejected" if changed else "not_pending", "kind": str(kind or "memory").casefold(), "proposal_id": proposal_id}


def inbox_feedback(
    config: AppConfig,
    *,
    memory_id: str,
    feedback_type: str,
    note: str = "",
    retrieval_id: str = "",
    agent_id: str = "",
) -> dict[str, Any]:
    bootstrap()
    from feedback_memory import record_feedback

    root, conn = _ready(config)
    conn.close()
    result = record_feedback(
        root,
        claim_id=memory_id,
        feedback_type=str(feedback_type).casefold(),
        source=f"agent:{origin_agent_id(agent_id)}",
        note=note,
        retrieval_uid=retrieval_id,
    )
    return result
