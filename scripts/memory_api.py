#!/usr/bin/env python3
"""Small local HTTP boundary for a centrally hosted Meta Memory store.

Clients authenticate with a bearer token defined outside the repository.  A
token binds profile, agent and allowed workspaces, so request JSON cannot
impersonate another agent by changing identity fields.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from _common import store_root
from feedback_memory import record_feedback
from ingest_raw_event import insert_raw_event
from memory_runtime import remember_memory
from proposal_manager import approve_memory_proposal, get_proposal, list_proposals
from retrieve_memories import retrieve


@dataclass(frozen=True)
class Principal:
    profile_id: str
    agent_id: str
    workspaces: frozenset[str]
    permissions: frozenset[str]


def load_principals(path: Path) -> dict[str, Principal]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    values = raw.get("agents") if isinstance(raw, dict) else None
    if not isinstance(values, dict):
        raise ValueError("agents file must contain an agents object")
    principals: dict[str, Principal] = {}
    for name, item in values.items():
        if not isinstance(item, dict):
            continue
        token = os.environ.get(str(item.get("token_env") or ""), "")
        if not token:
            continue
        principals[token] = Principal(
            profile_id=str(item.get("profile_id") or ""), agent_id=str(item.get("agent_id") or name),
            workspaces=frozenset(str(value) for value in item.get("workspaces", []) if str(value)),
            permissions=frozenset(str(value) for value in item.get("permissions", []) if str(value)),
        )
    if not principals:
        raise ValueError("No usable agent token found; set token_env values before starting the API.")
    return principals


def authorize(tokens: dict[str, Principal], header: str, *, permission: str, workspace_id: str) -> Principal:
    token = header.removeprefix("Bearer ").strip()
    principal = next((value for candidate, value in tokens.items() if hmac.compare_digest(candidate, token)), None)
    if principal is None:
        raise PermissionError("invalid bearer token")
    if permission not in principal.permissions:
        raise PermissionError(f"agent lacks {permission} permission")
    if workspace_id not in principal.workspaces:
        raise PermissionError("workspace is not allowed for this agent")
    return principal


def identity(payload: dict[str, Any], principal: Principal) -> tuple[str, str, str]:
    requested_profile = str(payload.get("profile_id") or principal.profile_id)
    requested_agent = str(payload.get("agent_id") or principal.agent_id)
    workspace = str(payload.get("workspace_id") or "")
    if requested_profile != principal.profile_id or requested_agent != principal.agent_id:
        raise PermissionError("request identity conflicts with token binding")
    if not workspace:
        raise ValueError("workspace_id is required")
    return requested_profile, workspace, requested_agent


def retrieval_args(root: Path, payload: dict[str, Any], profile_id: str, workspace_id: str, agent_id: str) -> argparse.Namespace:
    principal_subject = str(payload.get("principal_subject_id") or payload.get("subject_id") or "")
    if not principal_subject:
        raise ValueError("principal_subject_id or subject_id is required")
    active = [str(value) for value in payload.get("active_subject_ids", []) if str(value)]
    return argparse.Namespace(
        store=str(root), query=str(payload.get("query") or ""), query_file=None,
        top_k=max(1, int(payload.get("top_k", 6))), candidate_pool=max(1, int(payload.get("candidate_pool", 24))),
        expand_hops=max(0, min(2, int(payload.get("expand_hops", 1)))), session_id=str(payload.get("session_id") or ""),
        workspace_id=workspace_id, profile_id=profile_id, agent_id=agent_id, active_subject_id=active,
        valid_at=payload.get("valid_at"), no_chunks=bool(payload.get("no_chunks", False)),
        include_embeddings=bool(payload.get("include_embeddings", False)), embedding_model="external", rrf_k=60,
        subject_id=principal_subject, subject_name=None, domain=list(payload.get("domains") or []),
        memory_kind=list(payload.get("memory_kinds") or []), include_candidates=bool(payload.get("include_candidates", False)),
        no_basics=bool(payload.get("no_basics", False)),
    )


def remember_args(root: Path, payload: dict[str, Any], profile_id: str, workspace_id: str, agent_id: str) -> argparse.Namespace:
    content = str(payload.get("content") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not content or not title:
        raise ValueError("title and content are required")
    return argparse.Namespace(
        store=str(root), subject_id=str(payload.get("subject_id") or ""), subject_name=str(payload.get("subject_name") or "Unknown"),
        session_id=str(payload.get("session_id") or ""), profile_id=profile_id, workspace_id=workspace_id, agent_id=agent_id,
        visibility_scope=str(payload.get("visibility_scope") or "workspace"), shared_mode=True, title=title, title_file=None,
        content=content, content_file=None, payload_file=None, force_kind=payload.get("force_kind"), use_underlying_kind=bool(payload.get("use_underlying_kind", False)),
        domain=payload.get("domain"), topic=payload.get("topic"), source=None, start_at=payload.get("start_at"), end_at=payload.get("end_at"),
        confidence=payload.get("confidence"), importance=payload.get("importance"), status=None, tag=list(payload.get("tags") or []),
        related_person=list(payload.get("related_people") or []), related_event=list(payload.get("related_events") or []),
        related_topic=list(payload.get("related_topics") or []), related_source=list(payload.get("related_sources") or []), slug=None, mode="create",
        topic_hint=str(payload.get("topic_hint") or ""), domain_hint=str(payload.get("domain_hint") or ""), source_ref=str(payload.get("source_ref") or ""),
        event_time=str(payload.get("event_time") or ""), skip_raw_record=False, allow_duplicate=bool(payload.get("allow_duplicate", False)),
        skip_index=False, out_file=None,
    )


class MemoryAPI(BaseHTTPRequestHandler):
    server: "APIServer"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 2_000_000:
            raise ValueError("request body is required and must be below 2 MB")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON object required")
        return value

    def _reply(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(encoded))); self.end_headers(); self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._reply(HTTPStatus.OK, {"status": "ok"}); return
        self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            payload = self._json_body()
            workspace = str(payload.get("workspace_id") or "")
            permission = "read" if self.path == "/v1/retrieve" else "record" if self.path == "/v1/events" else "remember" if self.path == "/v1/remember" else "feedback" if self.path == "/v1/feedback" else "proposals" if self.path.startswith("/v1/proposals") else ""
            if not permission:
                self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"}); return
            principal = authorize(self.server.tokens, self.headers.get("Authorization", ""), permission=permission, workspace_id=workspace)
            profile_id, workspace_id, agent_id = identity(payload, principal)
            if self.path == "/v1/retrieve":
                result = retrieve(retrieval_args(self.server.root, payload, profile_id, workspace_id, agent_id))
            elif self.path == "/v1/events":
                content = str(payload.get("content") or "").strip()
                subject_id = str(payload.get("subject_id") or "")
                if not content or not subject_id:
                    raise ValueError("subject_id and content are required")
                result = insert_raw_event(
                    self.server.root, subject_id=subject_id, subject_name=str(payload.get("subject_name") or "Unknown"),
                    session_id=str(payload.get("session_id") or ""), source_type=str(payload.get("source_type") or "api"),
                    source_ref=str(payload.get("source_ref") or ""), topic_hint=str(payload.get("topic_hint") or ""), domain_hint=str(payload.get("domain_hint") or ""),
                    event_time=str(payload.get("event_time") or ""), content=content, allow_duplicate=bool(payload.get("allow_duplicate", False)),
                    profile_id=profile_id, workspace_id=workspace_id, origin_agent_id=agent_id,
                    visibility_scope=str(payload.get("visibility_scope") or "workspace"), event_uid=str(payload.get("event_uid") or ""),
                    idempotency_key=str(payload.get("idempotency_key") or ""), shared_mode=True,
                )
            elif self.path == "/v1/remember":
                result = remember_memory(remember_args(self.server.root, payload, profile_id, workspace_id, agent_id))
            elif self.path == "/v1/feedback":
                result = record_feedback(self.server.root, claim_id=str(payload.get("claim_id") or ""), feedback_type=str(payload.get("feedback_type") or ""), source=f"api:{agent_id}", note=str(payload.get("note") or ""), retrieval_uid=str(payload.get("retrieval_uid") or ""))
            elif self.path == "/v1/proposals/list":
                result = {"status": "ok", "proposals": list_proposals(self.server.root)}
            elif self.path.startswith("/v1/proposals/") and self.path.endswith("/approve"):
                proposal_id = self.path.removeprefix("/v1/proposals/").removesuffix("/approve").strip("/")
                proposal = get_proposal(self.server.root, proposal_id)
                if not proposal:
                    self._reply(HTTPStatus.NOT_FOUND, {"error": "proposal_not_found"}); return
                plan = proposal.get("plan", {})
                if str(plan.get("profile_id") or profile_id) != profile_id or str(plan.get("workspace_id") or workspace_id) != workspace_id:
                    raise PermissionError("proposal is outside this token scope")
                result = approve_memory_proposal(self.server.root, proposal_id)
            else:
                self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"}); return
            self._reply(HTTPStatus.OK, result)
        except PermissionError as exc:
            self._reply(HTTPStatus.FORBIDDEN, {"error": "forbidden", "detail": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            self._reply(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "detail": str(exc)})
        except Exception:
            self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"})


class APIServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], root: Path, tokens: dict[str, Principal]):
        super().__init__(address, MemoryAPI); self.root = root; self.tokens = tokens


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local authenticated Meta Memory HTTP API.")
    parser.add_argument("--store"); parser.add_argument("--agents-file", required=True, help="Private JSON file based on config/agents.example.json")
    parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(); root = store_root(args.store); tokens = load_principals(Path(args.agents_file).expanduser().resolve())
    server = APIServer((args.host, args.port), root, tokens)
    print(json.dumps({"status": "listening", "host": args.host, "port": args.port, "store": str(root)}, ensure_ascii=False), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
