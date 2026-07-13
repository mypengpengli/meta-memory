"""Explicit resource import; imported files remain source evidence."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .config import AppConfig
from .legacy import bootstrap
from .project_detection import resolve_project


def import_file(config: AppConfig, *, file_path: str | Path, project_name: str = "auto", session: str = "", start: str | Path | None = None) -> dict[str, Any]:
    bootstrap()
    from _common import compose_markdown, ensure_store_ready
    from ingest_raw_event import insert_raw_event
    from ingest_resource import resource_text

    source = Path(file_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Import file does not exist: {source}")
    project = resolve_project(config, project_name, start)
    root = Path(config.store)
    ensure_store_ready(root)
    content = resource_text(source, 20000)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    card = root / "resources" / f"{digest[:16]}.md"
    card.write_text(compose_markdown({"schema_version": 2, "resource_hash": digest, "source_path": str(source), "source_type": source.suffix.casefold().lstrip("."), "subject_id": config.subject_id, "project": project.project_id}, f"# {source.name}\n\nImported source evidence. It is not a user fact.\n"), encoding="utf-8")
    event = insert_raw_event(root, subject_id=config.subject_id, subject_name=config.user_name, session_id=session, source_type="resource", source_ref=str(source), topic_hint=project.project_id, domain_hint="resource", content=content, profile_id=config.profile_id, workspace_id=project.workspace_id, origin_agent_id="meta-memory", visibility_scope="workspace", shared_mode=bool(session))
    return {"status": "ok", "project": project.project_id, "resource_card": str(card), "raw_event": event, "truncated": len(content) >= 20000}
