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


def origin_agent_id(explicit: str | None = None) -> str:
    """Resolve provenance consistently for every public runtime operation.

    A launcher may pass an explicit identity, while direct invocations can use
    ``META_MEMORY_AGENT_ID``.  The generic fallback intentionally avoids
    pretending that the memory service itself authored the event.
    """
    return str(explicit or "").strip() or os.environ.get("META_MEMORY_AGENT_ID", "").strip() or "generic-agent"


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


def before(
    config: AppConfig,
    *,
    query: str,
    session: str = "auto",
    project_name: str = "auto",
    start: str | Path | None = None,
    agent_id: str = "",
    turn_uid: str = "",
) -> dict[str, Any]:
    """Durably begin a turn before asking the retrieval layer for context."""
    bootstrap()
    from .turn_service import begin_turn

    return begin_turn(
        config,
        query=query,
        project_name=project_name,
        requested_session=session,
        agent_id=origin_agent_id(agent_id),
        cwd=start,
        requested_turn_uid=turn_uid,
    )


def after(
    config: AppConfig,
    *,
    assistant_text: str,
    turn_uid: str = "",
    user_text: str = "",
    session: str = "",
    project_name: str = "auto",
    start: str | Path | None = None,
    agent_id: str = "",
) -> dict[str, Any]:
    """Complete a durable turn, with one-release compatibility for old calls."""
    if not assistant_text.strip():
        raise ValueError("Assistant content is required via --assistant or --assistant-file.")
    bootstrap()
    from .turn_service import begin_turn, complete_turn

    if turn_uid.strip():
        return complete_turn(config, turn_uid=turn_uid, assistant_text=assistant_text)
    if not user_text.strip() or not session.strip():
        raise ValueError("--turn is required, or provide the legacy --session and --user/--user-file arguments.")

    # Keep integrations on the old after contract working while ensuring they
    # receive the same durable, idempotent Turn semantics as new integrations.
    started = begin_turn(
        config,
        query=user_text,
        project_name=project_name,
        requested_session=session,
        agent_id=origin_agent_id(agent_id),
        cwd=start,
    )
    completed = complete_turn(config, turn_uid=str(started["turn_id"]), assistant_text=assistant_text)
    completed.update(
        {
            "warning": "legacy_after_arguments",
            "session_id": str(started["session_id"]),
            "project": str(started["project"]),
            "user_event": {"inserted": not bool(started.get("idempotent")), "raw_event_id": started.get("user_event_id")},
            "assistant_event": {"inserted": not bool(completed.get("idempotent")), "raw_event_id": completed.get("assistant_event_id")},
            "review": {"job_id": completed.get("review_job_id"), "status": "pending" if completed.get("queued") else "not_scheduled"},
        }
    )
    return completed


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
