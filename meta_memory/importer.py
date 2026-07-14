"""Import local reference material as auditable, non-factual resource evidence."""
from __future__ import annotations

import hashlib
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
