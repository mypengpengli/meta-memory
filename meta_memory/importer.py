"""Import local reference material as auditable, non-factual resource evidence."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from .config import AppConfig
from .legacy import bootstrap
from .project_detection import resolve_project


def _chunks(content: str, *, size: int = 3000) -> list[tuple[int, int, str]]:
    pieces: list[tuple[int, int, str]] = []
    start = 0
    while start < len(content):
        end = min(len(content), start + size)
        # Prefer a paragraph/line boundary without making tiny chunks.
        if end < len(content):
            boundary = max(content.rfind("\n\n", start + size // 2, end), content.rfind("\n", start + size // 2, end))
            if boundary > start:
                end = boundary
        piece = content[start:end].strip()
        if piece:
            pieces.append((start, end, piece))
        start = max(end, start + 1)
    return pieces


def import_file(
    config: AppConfig,
    *,
    file_path: str | Path,
    project_name: str = "auto",
    start: str | Path | None = None,
    agent_id: str = "generic-agent",
) -> dict[str, Any]:
    bootstrap()
    from _common import compose_markdown, ensure_store_ready, open_db
    from background_review import enqueue_review
    from ingest_raw_event import insert_raw_event_with_conn
    from ingest_resource import resource_text
    from security_scan import findings_json, scan_memory_content, security_state

    source = Path(file_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Import file does not exist: {source}")
    project = resolve_project(config, project_name, start)
    root = Path(config.store)
    ensure_store_ready(root)
    raw_bytes = source.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    # The indexed chunks retain large resources; the Raw Event has a bounded
    # audit preview so routine maintenance never needs to load megabytes.
    # Give the normalizer a size-derived ceiling rather than a fixed 20k/2M
    # truncation.  Large sources are retained as bounded chunks below.
    content = resource_text(source, max(4096, len(raw_bytes) * 4 + 4096))
    preview = content[:12000]
    source_session = f"resource:{digest[:16]}"
    resource_uid = hashlib.sha256(f"{config.profile_id}\x1f{project.workspace_id}\x1f{config.subject_id}\x1f{digest}".encode("utf-8")).hexdigest()
    card = root / "resources" / f"{digest[:16]}.md"
    stat = source.stat()
    source_metadata = {
        "path": str(source),
        "name": source.name,
        "type": source.suffix.casefold().lstrip("."),
        "content_hash": digest,
        "byte_size": len(raw_bytes),
        "modified_at": stat.st_mtime,
        "encoding": "utf-8-sig",
        "session_id": source_session,
    }
    findings = scan_memory_content(content, source_type="resource")
    security, _ = security_state(findings)

    conn = open_db(root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT resource_uid,raw_event_id,card_path FROM resource_imports WHERE profile_id=? AND workspace_id=? AND subject_id=? AND content_hash=?",
            (config.profile_id, project.workspace_id, config.subject_id, digest),
        ).fetchone()
        if existing:
            conn.commit()
            return {
                "status": "ok", "project": project.project_id, "resource_uid": str(existing[0]),
                "resource_card": str(existing[2] or card), "raw_event": {"inserted": False, "raw_event_id": int(existing[1] or 0)},
                "deduplicated": True, "chunks": 0, "source": source_metadata,
            }
        event = insert_raw_event_with_conn(
            conn,
            root,
            subject_id=config.subject_id,
            subject_name=config.user_name,
            session_id=source_session,
            source_type="resource",
            source_ref=str(source),
            topic_hint=project.project_id,
            domain_hint="resource",
            content=f"[Imported resource: {source.name}; sha256={digest}]\n\n{preview}",
            profile_id=config.profile_id,
            workspace_id=project.workspace_id,
            origin_agent_id=agent_id,
            visibility_scope="workspace",
            event_uid=f"resource:{digest}",
            idempotency_key=f"resource:{digest}",
            shared_mode=True,
        )
        event_id = int(event["raw_event_id"])
        conn.execute(
            """
            INSERT INTO resource_imports(
                resource_uid,profile_id,workspace_id,subject_id,source_path,source_name,source_type,
                content_hash,byte_size,modified_at,encoding,raw_event_id,card_path,updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'utf-8-sig', ?, ?, CURRENT_TIMESTAMP)
            """,
            (resource_uid, config.profile_id, project.workspace_id, config.subject_id, str(source), source.name, source.suffix.casefold().lstrip("."), digest, len(raw_bytes), stat.st_mtime, event_id, str(card)),
        )
        chunk_rows = _chunks(content)
        for index, (offset_start, offset_end, value) in enumerate(chunk_rows):
            conn.execute(
                "INSERT INTO resource_chunks(chunk_uid,resource_uid,chunk_index,content,content_hash,start_offset,end_offset) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), resource_uid, index, value, hashlib.sha256(value.encode("utf-8")).hexdigest(), offset_start, offset_end),
            )
        review = enqueue_review(
            root,
            subject_id=config.subject_id,
            session_id=source_session,
            event_start_id=event_id,
            event_end_id=event_id,
            trigger_type="resource_import",
            profile_id=config.profile_id,
            workspace_id=project.workspace_id,
            origin_agent_id=agent_id,
            conn=conn,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text(
        compose_markdown(
            {
                "schema_version": 3,
                "resource_uid": resource_uid,
                "resource_hash": digest,
                "source_path": source_metadata["path"],
                "source_type": source_metadata["type"],
                "subject_id": config.subject_id,
                "project": project.project_id,
                "verification_state": "resource",
                "prompt_eligible": False,
                "security_state": security,
                "security_findings": findings_json(findings),
            },
            f"# {source.name}\n\nImported source evidence. It is not a user fact and is not automatically used as prompt context.\n\n- SHA-256: `{digest}`\n- Bytes: {len(raw_bytes)}\n- Modified at: `{stat.st_mtime}`\n- Chunks: {len(chunk_rows)}\n- Encoding: `utf-8-sig`\n- Synthetic session: `{source_session}`\n- Security state: `{security}`\n",
        ),
        encoding="utf-8",
    )
    return {
        "status": "ok", "project": project.project_id, "resource_uid": resource_uid,
        "resource_card": str(card), "raw_event": event, "review": review,
        "chunks": len(chunk_rows), "truncated_preview": len(content) > len(preview),
        "security_state": security, "deduplicated": False, "source": source_metadata,
    }


def _resource_scope(config: AppConfig, *, project_name: str, start: str | Path | None):
    return resolve_project(config, project_name, start)


def _ensure_resource_store(config: AppConfig):
    bootstrap()
    from _common import ensure_store_ready, open_db

    root = Path(config.store)
    ensure_store_ready(root)
    return root, open_db(root)


def _supported_paths(source: Path, *, recursive: bool) -> list[Path]:
    bootstrap()
    from ingest_resource import SUPPORTED

    if source.is_file():
        return [source]
    if not source.is_dir():
        raise FileNotFoundError(f"Import path does not exist: {source}")
    if not recursive:
        raise ValueError("Importing a directory requires --recursive.")
    return sorted(
        [path for path in source.rglob("*") if path.is_file() and path.suffix.casefold() in SUPPORTED],
        key=lambda item: str(item).casefold(),
    )


def import_paths(
    config: AppConfig,
    *,
    path: str | Path,
    recursive: bool = False,
    changed_only: bool = False,
    project_name: str = "auto",
    start: str | Path | None = None,
    agent_id: str = "generic-agent",
) -> dict[str, Any]:
    """Import a file or supported directory tree with explicit skip results."""
    source = Path(path).expanduser().resolve()
    files = _supported_paths(source, recursive=recursive)
    project = _resource_scope(config, project_name=project_name, start=start)
    _, conn = _ensure_resource_store(config)
    try:
        existing = {
            (str(row[0]), str(row[1]))
            for row in conn.execute(
                "SELECT source_path,content_hash FROM resource_imports WHERE profile_id=? AND workspace_id=? AND subject_id=?",
                (config.profile_id, project.workspace_id, config.subject_id),
            )
        }
    finally:
        conn.close()
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for item in files:
        try:
            digest = hashlib.sha256(item.read_bytes()).hexdigest()
        except OSError as exc:
            failed.append({"path": str(item), "error": str(exc)})
            continue
        if changed_only and (str(item), digest) in existing:
            skipped.append({"path": str(item), "reason": "unchanged"})
            continue
        try:
            imported.append(
                import_file(
                    config, file_path=item, project_name=project_name, start=start, agent_id=agent_id,
                )
            )
        except (OSError, ValueError, RuntimeError) as exc:
            failed.append({"path": str(item), "error": str(exc)})
    return {
        "status": "ok" if not failed else "partial",
        "project": project.project_id,
        "source": str(source),
        "recursive": bool(recursive),
        "changed_only": bool(changed_only),
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "counts": {"found": len(files), "imported": len(imported), "skipped": len(skipped), "failed": len(failed)},
    }


def resource_list(
    config: AppConfig,
    *,
    project_name: str = "auto",
    start: str | Path | None = None,
    limit: int = 100,
    all_projects: bool = False,
) -> dict[str, Any]:
    project = _resource_scope(config, project_name=project_name, start=start)
    _, conn = _ensure_resource_store(config)
    try:
        clauses = ["profile_id=?", "subject_id=?"]
        params: list[object] = [config.profile_id, config.subject_id]
        if not all_projects:
            clauses.append("workspace_id=?")
            params.append(project.workspace_id)
        rows = conn.execute(
            "SELECT resource_uid,workspace_id,source_path,source_name,source_type,content_hash,byte_size,modified_at,encoding,raw_event_id,card_path,created_at,updated_at "
            "FROM resource_imports WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC LIMIT ?",
            (*params, max(1, min(int(limit), 500))),
        ).fetchall()
        values = [
            {
                "id": str(row[0]), "resource_uid": str(row[0]), "workspace_id": str(row[1]),
                "source_path": str(row[2]), "source_name": str(row[3]), "source_type": str(row[4]),
                "content_hash": str(row[5]), "byte_size": int(row[6] or 0), "modified_at": float(row[7] or 0.0),
                "encoding": str(row[8] or ""), "raw_event_id": int(row[9] or 0), "card_path": str(row[10] or ""),
                "created_at": str(row[11] or ""), "updated_at": str(row[12] or ""),
            }
            for row in rows
        ]
    finally:
        conn.close()
    return {"status": "ok", "project": project.project_id, "resources": values, "returned": len(values), "all_projects": bool(all_projects)}


def _resource_row(conn, config: AppConfig, resource_id: str, workspace_id: str, *, all_projects: bool = False):
    clauses = ["resource_uid=?", "profile_id=?", "subject_id=?"]
    params: list[object] = [resource_id, config.profile_id, config.subject_id]
    if not all_projects:
        clauses.append("workspace_id=?")
        params.append(workspace_id)
    row = conn.execute(
        "SELECT resource_uid,workspace_id,source_path,source_name,source_type,content_hash,byte_size,modified_at,encoding,raw_event_id,card_path,created_at,updated_at "
        "FROM resource_imports WHERE " + " AND ".join(clauses),
        params,
    ).fetchone()
    if not row:
        raise ValueError("Resource was not found in the selected project scope.")
    return row


def resource_show(
    config: AppConfig,
    *,
    resource_id: str,
    project_name: str = "auto",
    start: str | Path | None = None,
    all_projects: bool = False,
    chunk_limit: int = 5,
) -> dict[str, Any]:
    project = _resource_scope(config, project_name=project_name, start=start)
    _, conn = _ensure_resource_store(config)
    try:
        row = _resource_row(conn, config, resource_id, project.workspace_id, all_projects=all_projects)
        chunks = [
            {"id": str(item[0]), "index": int(item[1]), "start_offset": int(item[2]), "end_offset": int(item[3]), "content": str(item[4])}
            for item in conn.execute(
                "SELECT chunk_uid,chunk_index,start_offset,end_offset,content FROM resource_chunks WHERE resource_uid=? ORDER BY chunk_index LIMIT ?",
                (resource_id, max(1, min(int(chunk_limit), 50))),
            )
        ]
        count = int(conn.execute("SELECT COUNT(*) FROM resource_chunks WHERE resource_uid=?", (resource_id,)).fetchone()[0])
    finally:
        conn.close()
    resource = {
        "id": str(row[0]), "resource_uid": str(row[0]), "workspace_id": str(row[1]), "source_path": str(row[2]),
        "source_name": str(row[3]), "source_type": str(row[4]), "content_hash": str(row[5]), "byte_size": int(row[6] or 0),
        "modified_at": float(row[7] or 0.0), "encoding": str(row[8] or ""), "raw_event_id": int(row[9] or 0),
        "card_path": str(row[10] or ""), "created_at": str(row[11] or ""), "updated_at": str(row[12] or ""),
    }
    return {"status": "ok", "project": project.project_id, "resource": resource, "chunk_count": count, "chunks": chunks}


def resource_remove(
    config: AppConfig,
    *,
    resource_id: str,
    project_name: str = "auto",
    start: str | Path | None = None,
    all_projects: bool = False,
) -> dict[str, Any]:
    project = _resource_scope(config, project_name=project_name, start=start)
    root, conn = _ensure_resource_store(config)
    try:
        row = _resource_row(conn, config, resource_id, project.workspace_id, all_projects=all_projects)
        card = Path(str(row[10] or "")) if str(row[10] or "") else None
        conn.execute("BEGIN IMMEDIATE")
        chunks = conn.execute("DELETE FROM resource_chunks WHERE resource_uid=?", (resource_id,)).rowcount
        removed = conn.execute("DELETE FROM resource_imports WHERE resource_uid=?", (resource_id,)).rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    card_removed = False
    if card and card.is_file():
        try:
            card.resolve().relative_to(root.resolve())
            card.unlink()
            card_removed = True
        except (OSError, ValueError):
            card_removed = False
    return {
        "status": "ok", "project": project.project_id, "resource_id": resource_id,
        "removed": bool(removed), "chunks_removed": int(chunks), "card_removed": card_removed,
        "evidence_retained": True,
    }


def resource_refresh(
    config: AppConfig,
    *,
    resource_id: str,
    project_name: str = "auto",
    start: str | Path | None = None,
    agent_id: str = "generic-agent",
) -> dict[str, Any]:
    project = _resource_scope(config, project_name=project_name, start=start)
    _, conn = _ensure_resource_store(config)
    try:
        row = _resource_row(conn, config, resource_id, project.workspace_id)
        source_path = str(row[2])
    finally:
        conn.close()
    source = Path(source_path)
    if not source.is_file():
        raise FileNotFoundError(f"The imported resource source no longer exists: {source}")
    refreshed = import_file(config, file_path=source, project_name=project_name, start=start, agent_id=agent_id)
    replacement = str(refreshed.get("resource_uid") or resource_id)
    removed_previous: dict[str, Any] | None = None
    if replacement != resource_id:
        removed_previous = resource_remove(config, resource_id=resource_id, project_name=project_name, start=start)
    return {
        "status": "ok", "project": project.project_id, "resource_id": resource_id,
        "replacement_resource_id": replacement, "refreshed": refreshed, "removed_previous": removed_previous,
    }


def resource_export(
    config: AppConfig,
    *,
    project_name: str = "auto",
    start: str | Path | None = None,
    output: str | Path | None = None,
    format: str = "json",
    all_projects: bool = False,
) -> dict[str, Any]:
    normalized = str(format or "json").casefold()
    if normalized not in {"json", "markdown"}:
        raise ValueError("Resource export format must be json or markdown.")
    listing = resource_list(config, project_name=project_name, start=start, limit=500, all_projects=all_projects)
    resources = list(listing["resources"])
    if normalized == "json":
        content = json.dumps({"schema_version": 1, "project": listing["project"], "resources": resources}, ensure_ascii=False, indent=2) + "\n"
    else:
        content = "\n\n".join(
            "\n".join([
                f"# {item['source_name']}", "", f"- id: {item['id']}", f"- path: {item['source_path']}",
                f"- type: {item['source_type']}", f"- bytes: {item['byte_size']}", f"- updated_at: {item['updated_at']}",
            ])
            for item in resources
        ) + ("\n" if resources else "")
    destination = None
    if output:
        destination = Path(output).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    return {
        "status": "ok", "project": listing["project"], "format": normalized, "exported": len(resources),
        "output": str(destination) if destination else None, "content": None if destination else content,
    }
