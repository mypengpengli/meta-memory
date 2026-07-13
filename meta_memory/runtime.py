"""Public, three-concept runtime: user, project, and session."""
from __future__ import annotations

import argparse
import os
import uuid
from pathlib import Path
from typing import Any

from .config import AppConfig
from .legacy import bootstrap
from .project_detection import ProjectContext, resolve_project


def read_text(value: str | None = None, path: str | Path | None = None) -> str:
    if path:
        return Path(path).expanduser().read_text(encoding="utf-8-sig").strip()
    return str(value or "").strip()


def origin_agent_id() -> str:
    """Keep an audit trail without turning agent identity into a user setting."""
    return os.environ.get("META_MEMORY_AGENT_ID", "meta-memory").strip() or "meta-memory"


def _base(config: AppConfig, project: ProjectContext, session: str, *, agent_id: str = "") -> dict[str, Any]:
    return {
        "store": str(config.store),
        "subject_id": config.subject_id,
        "subject_name": config.user_name,
        "session_id": session,
        "profile_id": config.profile_id,
        "workspace_id": project.workspace_id,
        "agent_id": agent_id,
        # Normal mode never creates an agent-visible record.  Agent identity is
        # retained in provenance only, so all agents share the same memory.
        "visibility_scope": "workspace",
        "shared_mode": True,
    }


def _remember_visibility(content: str) -> str:
    bootstrap()
    from extract_memory_units import structured_fields

    kind = str(structured_fields(content, "", "").get("unit_kind", ""))
    return "global" if kind == "profile" else "workspace"


def before(config: AppConfig, *, query: str, session: str, project_name: str = "auto", start: str | Path | None = None) -> dict[str, Any]:
    if not session.strip():
        raise ValueError("--session is required so concurrent agents never mix conversations.")
    if not query.strip():
        raise ValueError("A request is required via --query or --query-file.")
    bootstrap()
    from memory_runtime import prepare_context

    project = resolve_project(config, project_name, start)
    base = _base(config, project, session, agent_id="")
    # Display names may change over time; retrieval must be keyed by the
    # stable user id rather than hiding legacy claims named "Unknown".
    base["subject_name"] = ""
    args = argparse.Namespace(
        **base,
        query=query, query_file=None, topic_hint="", domain_hint="", source_ref="",
        event_time="", skip_record_query=True, allow_duplicate=False,
        skip_heartbeat=True, heartbeat_policy="conservative",
        heartbeat_interval_minutes=config.maintenance_interval_minutes,
        heartbeat_min_pending=3, heartbeat_max_events=20,
        top_k=config.top_k, candidate_pool=max(config.top_k * 4, 24),
        expand_hops=1, include_candidates=False, no_basics=False, raw_limit=3,
        skip_raw_evidence=False, context_token_budget=1800, context_out_file=None,
        out_file=None, hot_snapshot_policy="frozen",
    )
    result = prepare_context(args)
    return {
        "status": "ok",
        "hot_context": result["static_hot_context"],
        "context": result["context_markdown"],
        "session_id": session,
        "project": project.project_id,
        "project_root": str(project.root),
        "query_route": result["query_route"],
        "hot_memory_snapshot_hash": result["hot_memory_snapshot_hash"],
    }


def after(
    config: AppConfig,
    *,
    user_text: str,
    assistant_text: str,
    session: str,
    project_name: str = "auto",
    start: str | Path | None = None,
) -> dict[str, Any]:
    """Append a whole turn and enqueue work; never consolidate inline."""
    if not session.strip():
        raise ValueError("--session is required so concurrent agents never mix conversations.")
    if not user_text.strip() or not assistant_text.strip():
        raise ValueError("Both --user-file/--user and --assistant-file/--assistant are required.")
    bootstrap()
    from background_review import enqueue_review
    from ingest_raw_event import insert_raw_event

    project = resolve_project(config, project_name, start)
    base = _base(config, project, session, agent_id=origin_agent_id())
    event_identity = {
        "subject_id": config.subject_id, "subject_name": config.user_name,
        "session_id": session, "profile_id": config.profile_id,
        "workspace_id": project.workspace_id, "origin_agent_id": origin_agent_id(),
        "visibility_scope": "workspace", "shared_mode": True,
    }
    user_event = insert_raw_event(
        Path(base["store"]), **event_identity,
        source_type="conversation-user", content=user_text, source_ref="", topic_hint="", domain_hint="", event_time="",
    )
    assistant_event = insert_raw_event(
        Path(base["store"]), **event_identity,
        source_type="conversation-assistant", content=assistant_text, source_ref="", topic_hint="", domain_hint="", event_time="",
    )
    inserted = [int(item["raw_event_id"]) for item in (user_event, assistant_event) if item.get("inserted")]
    queued: dict[str, Any]
    if inserted:
        queued = enqueue_review(
            Path(base["store"]), subject_id=config.subject_id, session_id=session,
            event_start_id=min(inserted), event_end_id=max(inserted), trigger_type="turn_end",
            profile_id=config.profile_id, workspace_id=project.workspace_id,
            origin_agent_id=origin_agent_id(),
        )
    else:
        queued = {"status": "not_scheduled", "reason": "duplicate_turn"}
    return {"status": "ok", "session_id": session, "project": project.project_id, "user_event": user_event, "assistant_event": assistant_event, "review": queued}


def remember(
    config: AppConfig,
    *,
    content: str,
    title: str = "",
    session: str = "",
    project_name: str = "auto",
    start: str | Path | None = None,
) -> dict[str, Any]:
    if not content.strip():
        raise ValueError("Memory content is required via --content or --content-file.")
    bootstrap()
    from memory_runtime import remember_memory

    project = resolve_project(config, project_name, start)
    session = session.strip() or f"remember:{uuid.uuid4()}"
    base = _base(config, project, session, agent_id=origin_agent_id())
    base["visibility_scope"] = _remember_visibility(content)
    # A profile preference belongs to the user scope. Project decisions remain
    # in the current project scope; neither path is agent-private.
    if base["visibility_scope"] == "global":
        base["workspace_id"] = "global"
    args = argparse.Namespace(
        **base, title=title or content.splitlines()[0][:80], title_file=None,
        content=content, content_file=None, payload_file=None, force_kind=None,
        use_underlying_kind=True, domain=None, topic=None, source=None,
        start_at=None, end_at=None, confidence=None, importance=None, status=None,
        tag=[], related_person=[], related_event=[], related_topic=[], related_source=[],
        slug=None, mode="create", topic_hint="", domain_hint="", source_ref="",
        event_time="", skip_raw_record=False, allow_duplicate=False, skip_index=False,
        out_file=None,
    )
    result = remember_memory(args)
    result["project"] = project.project_id
    return result


def correct(config: AppConfig, *, memory_id: str, content: str) -> dict[str, Any]:
    if not memory_id.strip() or not content.strip():
        raise ValueError("--memory and correction content are required.")
    bootstrap()
    from feedback_memory import record_feedback

    # Feedback records replacement text as raw evidence and stages a reviewed
    # correction. It deliberately never overwrites a claim in place.
    return record_feedback(Path(config.store), claim_id=memory_id, feedback_type="incorrect", source="user", note=content)


def search(config: AppConfig, *, query: str, project_name: str = "auto", start: str | Path | None = None, limit: int | None = None) -> dict[str, Any]:
    bootstrap()
    from retrieve_memories import retrieve

    project = resolve_project(config, project_name, start)
    args = argparse.Namespace(
        store=str(config.store), query=query, query_file=None, top_k=limit or config.top_k,
        candidate_pool=max((limit or config.top_k) * 4, 24), expand_hops=1,
        session_id="", workspace_id=project.workspace_id, profile_id=config.profile_id,
        agent_id="", active_subject_id=[], valid_at=None, no_chunks=False,
        include_embeddings=config.embeddings, embedding_model="external", rrf_k=60,
        subject_id=config.subject_id, subject_name=None, domain=[], memory_kind=[],
        include_candidates=False, no_basics=False,
    )
    result = retrieve(args, query=query)
    return {"status": "ok", "project": project.project_id, "results": result.get("selected", []), "query": query}


def history(config: AppConfig, *, query: str, project_name: str = "auto", start: str | Path | None = None) -> dict[str, Any]:
    bootstrap()
    from session_search import discovery

    project = resolve_project(config, project_name, start)
    result = discovery(Path(config.store), subject_id=config.subject_id, query=query, limit=8, profile_id=config.profile_id, workspace_id=project.workspace_id)
    return {"status": "ok", "project": project.project_id, **result}
