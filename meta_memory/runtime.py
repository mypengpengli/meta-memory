"""Public, three-concept runtime: user, project, and session."""
from __future__ import annotations

import argparse
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .config import AppConfig
from .legacy import bootstrap
from .project_detection import ProjectContext, resolve_project


def read_text(
    value: str | None = None,
    path: str | Path | None = None,
    *,
    preserve: bool = False,
) -> str:
    if path:
        # Decode bytes directly so an exact assistant answer keeps CRLF and
        # leading/trailing whitespace.  Callers validate emptiness with
        # ``.strip()`` without mutating the persisted text.
        text = Path(path).expanduser().read_bytes().decode("utf-8-sig")
    else:
        text = str(value or "")
    return text if preserve else text.strip()


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
    from .scope_inference import inferred_visibility

    kind = str(structured_fields(content, "", "").get("unit_kind", ""))
    return inferred_visibility(content, unit_kind=kind, source_type="explicit-memory")


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
        if turn_uid.strip():
            stable_agent = origin_agent_id(agent_id)
            try:
                from .runtime_audit import record_turn_error

                record_turn_error(
                    config, agent_id=stable_agent, turn_uid=turn_uid, phase="after",
                    error_code="assistant_body_missing", error_message="assistant content was empty",
                )
            except Exception:
                pass
        raise ValueError("Assistant content is required via --assistant or --assistant-file.")
    bootstrap()
    from .turn_service import begin_turn, complete_turn

    if turn_uid.strip():
        try:
            explicit_agent = str(agent_id or "").strip() or os.environ.get("META_MEMORY_AGENT_ID", "").strip()
            stable_agent = origin_agent_id(explicit_agent)
            return complete_turn(config, turn_uid=turn_uid, assistant_text=assistant_text, agent_id=stable_agent if explicit_agent else "")
        except ValueError as exc:
            from .runtime_audit import record_turn_error

            text = str(exc).casefold()
            code = "wrong_turn_owner" if "different agent" in text else "turn_not_found" if "not found" in text else "response_hash_conflict" if "different assistant response" in text else "after_failed"
            try:
                record_turn_error(config, agent_id=stable_agent, turn_uid=turn_uid, phase="after", error_code=code, error_message=str(exc))
            except Exception:
                pass
            raise
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            from .spool import spool_completion
            from .runtime_audit import record_turn_error

            deferred = spool_completion(config, turn_uid=turn_uid, assistant_text=assistant_text, agent_id=stable_agent if explicit_agent else "", error=str(exc))
            try:
                record_turn_error(config, agent_id=stable_agent, turn_uid=turn_uid, phase="after", error_code="completion_spooled", error_message=str(exc))
            except Exception:
                pass
            return {**deferred, "warning": "assistant_response_spooled_for_retry"}
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
    completed = complete_turn(config, turn_uid=str(started["turn_id"]), assistant_text=assistant_text, agent_id=origin_agent_id(agent_id))
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
    agent_id: str = "",
    scope: str = "auto",
    source_kind: str = "user",
    source_ref: str = "",
) -> dict[str, Any]:
    if not content.strip():
        raise ValueError("Memory content is required via --content or --content-file.")
    bootstrap()
    from memory_runtime import remember_memory

    project = resolve_project(config, project_name, start)
    stable_agent = origin_agent_id(agent_id)
    from .session_manager import resolve_session

    resolved = resolve_session(config, requested=session or "auto", agent_id=stable_agent, project=project)
    source_kind = source_kind.strip().casefold() or "user"
    scope = scope.strip().casefold() or "auto"
    if scope not in {"auto", "user", "project"}:
        raise ValueError("--scope must be auto, user, or project.")
    if source_kind not in {"user", "agent-observation", "tool-result", "resource"}:
        raise ValueError("--source-kind must be user, agent-observation, tool-result, or resource.")
    if source_kind in {"agent-observation", "tool-result", "resource"}:
        label = {
            "agent-observation": "Agent observations",
            "tool-result": "Tool results",
            "resource": "Resources",
        }[source_kind]
        if not source_ref.strip():
            # A host tool call normally supplies a stable call id.  Keep the
            # documented UUID fallback for a standalone tool result, while an
            # observation asserted by an agent always needs inspectable proof.
            if source_kind == "agent-observation":
                raise ValueError("Agent observations require --source-ref evidence such as a commit, file, or command result.")
        if scope == "user":
            raise ValueError(f"{label} are project-scoped and cannot be saved as user memory.")
        scope = "project"
    visibility = "global" if scope == "user" else "workspace" if scope == "project" else _remember_visibility(content)
    base = _base(config, project, resolved.session_id, agent_id=stable_agent)
    base["visibility_scope"] = visibility
    if visibility == "global":
        base["workspace_id"] = "global"
    args = argparse.Namespace(
        **base, title=title or content.splitlines()[0][:80], title_file=None,
        content=content, content_file=None, payload_file=None, force_kind=None,
        use_underlying_kind=True, domain=None, topic=None, source=None,
        start_at=None, end_at=None, confidence=None, importance=None, status=None,
        tag=[], related_person=[], related_event=[], related_topic=[], related_source=[],
        slug=None, mode="create", topic_hint="", domain_hint="", source_ref=source_ref,
        event_time="", skip_raw_record=False, allow_duplicate=False, skip_index=False,
        out_file=None, source_kind=source_kind,
    )
    result = remember_memory(args)
    result["project"] = project.project_id
    if result.get("status") == "ok":
        from .runtime_audit import record_write
        try:
            record_write(config, agent_id=stable_agent, workspace_id=project.workspace_id)
        except Exception:
            pass
    return result


def correct(config: AppConfig, *, memory_id: str, content: str, agent_id: str = "") -> dict[str, Any]:
    if not memory_id.strip() or not content.strip():
        raise ValueError("--memory and correction content are required.")
    bootstrap()
    from _common import open_db, sha256_text
    from apply_memory_plan import apply_plan
    from extract_memory_units import structured_fields
    from ingest_raw_event import insert_raw_event
    from projection_outbox import process_projection_outbox

    root = Path(config.store)
    stable_agent = origin_agent_id(agent_id)
    conn = open_db(root)
    try:
        row = conn.execute(
            """
            SELECT id,subject_id,subject_name,memory_kind,domain,topic,title,confidence,
                   importance,durability,sensitivity,predicate,subject_text,object_text,
                   qualifiers_json,profile_id,workspace_id,origin_agent_id,visibility_scope,
                   owner_agent_id,status
            FROM claims WHERE id=?
            """,
            (memory_id,),
        ).fetchone()
        if not row:
            raise ValueError("Claim not found.")
        sources = [int(item[0]) for item in conn.execute("SELECT raw_event_id FROM claim_sources WHERE claim_id=? ORDER BY raw_event_id", (memory_id,))]
    finally:
        conn.close()
    keys = ["id", "subject_id", "subject_name", "memory_kind", "domain", "topic", "title", "confidence", "importance", "durability", "sensitivity", "predicate", "subject_text", "object_text", "qualifiers_json", "profile_id", "workspace_id", "origin_agent_id", "visibility_scope", "owner_agent_id", "status"]
    claim = dict(zip(keys, row))
    if str(claim["status"]) not in {"active", "candidate"}:
        raise ValueError("Only an active memory can be corrected.")
    if str(claim["visibility_scope"] or "workspace") == "agent" and str(claim["owner_agent_id"] or "") != stable_agent:
        raise ValueError("This is an agent-private claim owned by another Agent.")
    raw = insert_raw_event(
        root,
        subject_id=str(claim["subject_id"]),
        subject_name=str(claim["subject_name"] or config.user_name),
        session_id="",
        source_type="user-memory-feedback",
        source_ref=f"claim:{memory_id}",
        content=content,
        allow_duplicate=True,
        profile_id=str(claim["profile_id"]),
        workspace_id=str(claim["workspace_id"]),
        origin_agent_id=stable_agent,
        visibility_scope=str(claim["visibility_scope"] or "workspace"),
    )
    new_sources = list(dict.fromkeys([*sources, int(raw["raw_event_id"])]))
    fields = structured_fields(content, str(claim["topic"]), str(claim["domain"]))
    transition_terms = ("现在", "已经", "改成", "迁移", "不再", "升级", "now", "migrated", "switched")
    action_name = "SUPERSEDE" if str(claim["memory_kind"]) == "state" or any(term in content.casefold() for term in transition_terms) else "CORRECT"
    action = {
        "plan_id": f"explicit-correct:{memory_id}:{sha256_text(content)[:20]}",
        "action": action_name,
        "subject_id": str(claim["subject_id"]),
        "subject_name": str(claim["subject_name"] or config.user_name),
        "target_claim_id": memory_id,
        "source_event_ids": new_sources,
        "memory_kind": str(claim["memory_kind"]),
        "domain": str(fields["domain"] or claim["domain"]),
        "topic": str(fields["topic"] or claim["topic"]),
        "title": str(claim["title"]),
        "content": content,
        "confidence": max(0.95, float(claim["confidence"])),
        "importance": float(claim["importance"]),
        "durability": float(claim["durability"]),
        "sensitivity": str(claim["sensitivity"]),
        "predicate": str(fields["predicate"] or claim["predicate"]),
        "subject_text": str(fields["subject_text"] or claim["subject_text"]),
        "object_text": str(fields["object_text"] or content[:240]),
        "qualifiers": fields["qualifiers"],
        "profile_id": str(claim["profile_id"]),
        "workspace_id": str(claim["workspace_id"]),
        # The scope/owner remain attached to the claim, while this correction
        # is attributed to the Agent that actually issued it.
        "origin_agent_id": stable_agent,
        "visibility_scope": str(claim["visibility_scope"] or "workspace"),
        "owner_agent_id": str(claim["owner_agent_id"] or ""),
        "verification_state": "verified",
        "source_type": "user-memory-feedback",
        "origin": "explicit_correct",
        "explicit_user_action": True,
        "requires_review": False,
    }
    applied = apply_plan(root, {"schema_version": 3, "subject_id": claim["subject_id"], "policy": "automatic", "actions": [action]}, review_approved=True)
    result = applied.get("results", [{}])[0] if applied.get("results") else {}
    if applied.get("status") != "ok" or result.get("status") != "applied":
        raise RuntimeError("Could not apply the correction synchronously.")
    projection = process_projection_outbox(root, limit=20)
    return {
        "status": "ok",
        "old_claim_id": memory_id,
        "new_claim_id": result.get("claim_id"),
        "action": action_name,
        "raw_event_id": raw.get("raw_event_id"),
        "projection": projection,
        "immediately_readable": True,
    }


def search(
    config: AppConfig,
    *,
    query: str,
    project_name: str = "auto",
    start: str | Path | None = None,
    limit: int | None = None,
    agent_id: str = "",
) -> dict[str, Any]:
    bootstrap()
    from retrieve_memories import retrieve

    project = resolve_project(config, project_name, start)
    args = argparse.Namespace(
        store=str(config.store), query=query, query_file=None, top_k=limit or config.top_k,
        candidate_pool=max((limit or config.top_k) * 4, 24), expand_hops=1,
        session_id="", workspace_id=project.workspace_id, profile_id=config.profile_id,
        agent_id=origin_agent_id(agent_id), active_subject_id=[], valid_at=None, no_chunks=False,
        include_embeddings=config.embeddings, embedding_model="external", rrf_k=60,
        # ``meta-memory search`` is an explicit evidence lookup, so it may
        # return bounded chunks from imported resources.  ``before`` leaves
        # this false through memory_runtime._retrieval_args.
        include_resources=True,
        subject_id=config.subject_id, subject_name=None, domain=[], memory_kind=[],
        include_candidates=False, no_basics=False,
    )
    result = retrieve(args, query=query)
    return {"status": "ok", "project": project.project_id, "results": result.get("selected", []), "query": query}


def history(
    config: AppConfig,
    *,
    query: str,
    project_name: str = "auto",
    start: str | Path | None = None,
    agent_id: str = "",
    detail: bool = False,
) -> dict[str, Any]:
    bootstrap()
    from session_search import discover_session_summaries, read_session_detail

    project = resolve_project(config, project_name, start)
    scope = str(getattr(config, "history_scope", "workspace-summary") or "workspace-summary")
    reader_agent = origin_agent_id(agent_id)
    result = discover_session_summaries(
        Path(config.store),
        subject_id=config.subject_id,
        query=query,
        limit=8,
        profile_id=config.profile_id,
        workspace_id=project.workspace_id,
        agent_id=reader_agent if scope == "agent" else "",
    )
    response = {"status": "ok", "project": project.project_id, "history_scope": scope, **result}
    if detail:
        if not bool(getattr(config, "history_allow_detail", True)):
            raise ValueError("History detail is disabled by configuration.")
        response["mode"] = "detail"
        response["details"] = read_session_detail(
            Path(config.store),
            summaries=list(result["sessions"]),
            subject_id=config.subject_id,
            workspace_id=project.workspace_id,
            profile_id=config.profile_id,
            max_sessions=int(getattr(config, "history_detail_max_sessions", 3)),
            max_turns=int(getattr(config, "history_detail_max_turns", 8)),
            max_chars=int(getattr(config, "history_detail_max_chars", 12000)),
            tool_summary_max_chars=int(getattr(config, "history_tool_summary_max_chars", 1200)),
        )
    return response
