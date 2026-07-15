"""Public lifecycle operations for durable memory Claims.

The runtime's ``remember`` and ``correct`` commands intentionally own the
write path.  This module owns the human-facing management path around that
write path: inspection, archival, forgetting and export.  It talks to the
authoritative ``claims`` table rather than treating Markdown projections as
truth, so a lifecycle action remains correct even while a projection worker is
behind.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import AppConfig
from .legacy import bootstrap
from .project_detection import resolve_project


_CLAIM_COLUMNS = (
    "id,subject_id,subject_name,memory_kind,domain,topic,title,content,status,"
    "verification_state,confidence,importance,durability,sensitivity,valid_from,"
    "valid_to,observed_at,support_count,memory_path,predicate,subject_text,"
    "object_text,qualifiers_json,confirmed_utility,replaced_by,corrected_by,"
    "supersedes,security_state,prompt_eligible,profile_id,workspace_id,"
    "visibility_scope,owner_agent_id,origin_agent_id,created_at,updated_at"
)
_CLAIM_KEYS = _CLAIM_COLUMNS.split(",")


def _open(config: AppConfig):
    bootstrap()
    from _common import ensure_store_ready, open_db

    root = Path(config.store)
    ensure_store_ready(root)
    return root, open_db(root)


def _project_scope(config: AppConfig, project_name: str, start: str | Path | None):
    return resolve_project(config, project_name, start)


def _scope_sql(config: AppConfig, workspace_id: str, *, all_projects: bool) -> tuple[str, list[object]]:
    clauses = ["profile_id=?", "subject_id=?"]
    params: list[object] = [config.profile_id, config.subject_id]
    if not all_projects:
        # Explicit user memories live in the global workspace.  They should be
        # visible beside the current project without making unrelated projects
        # visible.
        clauses.append("(workspace_id=? OR workspace_id='global' OR visibility_scope='global')")
        params.append(workspace_id)
    return " AND ".join(clauses), params


def _claim_dict(row: object) -> dict[str, Any]:
    value = dict(zip(_CLAIM_KEYS, row))
    try:
        value["qualifiers"] = json.loads(str(value.pop("qualifiers_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        value["qualifiers"] = {}
    value["prompt_eligible"] = bool(value.get("prompt_eligible", 0))
    for field in ("confidence", "importance", "durability", "confirmed_utility"):
        try:
            value[field] = float(value.get(field) or 0.0)
        except (TypeError, ValueError):
            value[field] = 0.0
    value["support_count"] = int(value.get("support_count") or 0)
    return value


def _sources(conn, claim_id: str) -> list[int]:
    return [int(row[0]) for row in conn.execute("SELECT raw_event_id FROM claim_sources WHERE claim_id=? ORDER BY raw_event_id", (claim_id,))]


def _claim_in_scope(
    conn,
    config: AppConfig,
    claim_id: str,
    *,
    workspace_id: str,
    all_projects: bool = False,
) -> dict[str, Any]:
    scope, params = _scope_sql(config, workspace_id, all_projects=all_projects)
    row = conn.execute(
        f"SELECT {_CLAIM_COLUMNS} FROM claims WHERE id=? AND {scope}",
        (claim_id, *params),
    ).fetchone()
    if not row:
        raise ValueError("Memory claim was not found in the selected project scope.")
    claim = _claim_dict(row)
    claim["source_event_ids"] = _sources(conn, claim_id)
    return claim


def memory_list(
    config: AppConfig,
    *,
    project_name: str = "auto",
    start: str | Path | None = None,
    limit: int = 50,
    status: str = "",
    kind: str = "",
    all_projects: bool = False,
) -> dict[str, Any]:
    project = _project_scope(config, project_name, start)
    _, conn = _open(config)
    try:
        scope, params = _scope_sql(config, project.workspace_id, all_projects=all_projects)
        if status:
            scope += " AND LOWER(status)=?"
            params.append(status.casefold())
        if kind:
            scope += " AND LOWER(memory_kind)=?"
            params.append(kind.casefold())
        rows = conn.execute(
            f"SELECT {_CLAIM_COLUMNS} FROM claims WHERE {scope} "
            "ORDER BY COALESCE(updated_at,created_at) DESC, id DESC LIMIT ?",
            (*params, max(1, min(int(limit), 500))),
        ).fetchall()
        claims = [_claim_dict(row) for row in rows]
    finally:
        conn.close()
    return {
        "status": "ok",
        "project": project.project_id,
        "workspace_id": project.workspace_id,
        "all_projects": bool(all_projects),
        "returned": len(claims),
        "claims": claims,
    }


def memory_recent(
    config: AppConfig,
    *,
    project_name: str = "auto",
    start: str | Path | None = None,
    limit: int = 20,
    all_projects: bool = False,
) -> dict[str, Any]:
    return memory_list(
        config,
        project_name=project_name,
        start=start,
        limit=limit,
        status="active",
        all_projects=all_projects,
    ) | {"mode": "recent"}


def memory_show(
    config: AppConfig,
    *,
    memory_id: str,
    project_name: str = "auto",
    start: str | Path | None = None,
    all_projects: bool = False,
) -> dict[str, Any]:
    project = _project_scope(config, project_name, start)
    _, conn = _open(config)
    try:
        claim = _claim_in_scope(conn, config, memory_id, workspace_id=project.workspace_id, all_projects=all_projects)
        versions = [
            {"id": int(row[0]), "reason": str(row[1] or ""), "created_at": str(row[2] or "")}
            for row in conn.execute(
                "SELECT id,reason,created_at FROM memory_versions WHERE memory_path=? ORDER BY id DESC LIMIT 20",
                (str(claim.get("memory_path") or ""),),
            )
        ]
        feedback = [
            {
                "id": int(row[0]), "type": str(row[1]), "note": str(row[2] or ""),
                "created_at": str(row[3] or ""), "weight": float(row[4] or 0.0),
            }
            for row in conn.execute(
                "SELECT id,feedback_type,note,created_at,weight FROM memory_feedback WHERE claim_uid=? ORDER BY id DESC LIMIT 20",
                (memory_id,),
            )
        ]
        edges = [
            {"from": str(row[0]), "to": str(row[1]), "type": str(row[2])}
            for row in conn.execute(
                "SELECT from_claim_id,to_claim_id,edge_type FROM memory_edges WHERE from_claim_id=? OR to_claim_id=? ORDER BY id DESC LIMIT 30",
                (memory_id, memory_id),
            )
        ]
    finally:
        conn.close()
    return {"status": "ok", "project": project.project_id, "claim": claim, "versions": versions, "feedback": feedback, "edges": edges}


def _mark_dirty(conn, claim: dict[str, Any]) -> None:
    # Reuse the mature runtime invalidation contract.  It only records dirty
    # generations; a later heartbeat builds the derived views.
    from apply_memory_plan import mark_runtime_dirty

    mark_runtime_dirty(conn, claim)


def _update_document_visibility(conn, claim: dict[str, Any], *, state: str, delete: bool = False) -> int:
    memory_id = str(claim["id"])
    path = str(claim.get("memory_path") or "")
    if delete:
        doc_paths = [str(row[0]) for row in conn.execute("SELECT path FROM documents WHERE memory_id=? OR path=?", (memory_id, path))]
        for doc_path in doc_paths:
            conn.execute("DELETE FROM scores WHERE path=?", (doc_path,))
            conn.execute("DELETE FROM chunks WHERE doc_path=?", (doc_path,))
            try:
                conn.execute("DELETE FROM document_fts WHERE path=?", (doc_path,))
                conn.execute("DELETE FROM chunk_fts WHERE doc_path=?", (doc_path,))
            except Exception:
                # FTS is optional and may not be present in an embedded SQLite
                # build.  The authoritative document rows are still removed.
                pass
        changed = conn.execute("DELETE FROM documents WHERE memory_id=? OR path=?", (memory_id, path)).rowcount
        return int(changed)
    changed = conn.execute(
        "UPDATE documents SET status=?,prompt_eligible=0 WHERE memory_id=? OR path=?",
        (state, memory_id, path),
    ).rowcount
    return int(changed)


def _safe_unlink_memory_file(root: Path, memory_path: str) -> bool:
    if not memory_path:
        return False
    path = Path(memory_path)
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    if not path.is_file():
        return False
    path.unlink()
    return True


def _transition_memory(
    config: AppConfig,
    *,
    memory_id: str,
    state: str,
    project_name: str,
    start: str | Path | None,
    all_projects: bool,
    remove_projection: bool,
) -> dict[str, Any]:
    project = _project_scope(config, project_name, start)
    root, conn = _open(config)
    try:
        claim = _claim_in_scope(conn, config, memory_id, workspace_id=project.workspace_id, all_projects=all_projects)
        if str(claim.get("status") or "") == state:
            return {"status": "ok", "idempotent": True, "claim_id": memory_id, "state": state, "project": project.project_id}
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE claims SET status=?,prompt_eligible=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (state, memory_id),
        )
        claim["status"] = state
        claim["prompt_eligible"] = False
        documents_changed = _update_document_visibility(conn, claim, state=state, delete=remove_projection)
        _mark_dirty(conn, claim)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    file_removed = _safe_unlink_memory_file(root, str(claim.get("memory_path") or "")) if remove_projection else False
    return {
        "status": "ok", "claim_id": memory_id, "state": state, "project": project.project_id,
        "documents_changed": documents_changed, "projection_removed": bool(remove_projection), "memory_file_removed": file_removed,
    }


def memory_archive(
    config: AppConfig,
    *,
    memory_id: str,
    project_name: str = "auto",
    start: str | Path | None = None,
    all_projects: bool = False,
) -> dict[str, Any]:
    """Hide a Claim from recall while retaining its history and source links."""
    return _transition_memory(
        config, memory_id=memory_id, state="archived", project_name=project_name,
        start=start, all_projects=all_projects, remove_projection=False,
    )


def memory_forget(
    config: AppConfig,
    *,
    memory_id: str,
    project_name: str = "auto",
    start: str | Path | None = None,
    all_projects: bool = False,
) -> dict[str, Any]:
    """Remove a Claim from active and derived memory while retaining audit evidence."""
    return _transition_memory(
        config, memory_id=memory_id, state="forgotten", project_name=project_name,
        start=start, all_projects=all_projects, remove_projection=True,
    )


def _markdown(claim: dict[str, Any]) -> str:
    qualifiers = json.dumps(claim.get("qualifiers") or {}, ensure_ascii=False, indent=2)
    sources = "\n".join(f"- raw_event:{item}" for item in claim.get("source_event_ids") or []) or "- none"
    return "\n".join(
        [
            f"# {claim.get('title') or claim.get('id')}", "", str(claim.get("content") or ""), "",
            "## Metadata", "", f"- id: {claim.get('id')}", f"- status: {claim.get('status')}",
            f"- kind: {claim.get('memory_kind')}", f"- topic: {claim.get('topic')}",
            f"- updated_at: {claim.get('updated_at')}", "", "## Sources", "", sources,
            "", "## Qualifiers", "", "```json", qualifiers, "```", "",
        ]
    )


def memory_export(
    config: AppConfig,
    *,
    project_name: str = "auto",
    start: str | Path | None = None,
    output: str | Path | None = None,
    format: str = "json",
    status: str = "",
    all_projects: bool = False,
) -> dict[str, Any]:
    normalized = str(format or "json").casefold()
    if normalized not in {"json", "markdown"}:
        raise ValueError("Memory export format must be json or markdown.")
    listing = memory_list(
        config, project_name=project_name, start=start, limit=500,
        status=status, all_projects=all_projects,
    )
    claims = list(listing["claims"])
    root, conn = _open(config)
    try:
        for claim in claims:
            claim["source_event_ids"] = _sources(conn, str(claim["id"]))
    finally:
        conn.close()
    if normalized == "markdown":
        content = "\n\n".join(_markdown(claim) for claim in claims)
    else:
        content = json.dumps(
            {"schema_version": 1, "project": listing["project"], "claims": claims},
            ensure_ascii=False,
            indent=2,
            default=str,
        ) + "\n"
    destination = None
    if output:
        destination = Path(output).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    return {
        "status": "ok", "project": listing["project"], "format": normalized,
        "exported": len(claims), "output": str(destination) if destination else None,
        "content": None if destination else content,
        "store": str(root),
    }
