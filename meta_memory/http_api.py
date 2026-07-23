"""Authenticated, dependency-free HTTP transport for a central memory store.

The local runtime remains the source of truth.  This module only binds a
remote bearer principal to an explicit profile, Agent, workspace and subject,
then delegates to the same durable Turn and memory services used by the CLI.
It deliberately never derives a remote workspace or session from server cwd.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import signal
import ssl
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, TextIO
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .config import AppConfig, load_config
from .legacy import bootstrap


# The mature data plane is still shipped in the ``scripts`` package.  Bootstrap
# before importing compatibility modules so this module also works from an
# installed wheel and from a cwd outside the checkout.
bootstrap()
from _common import open_db  # type: ignore  # noqa: E402
from feedback_memory import record_feedback  # type: ignore  # noqa: E402
from ingest_raw_event import insert_raw_event  # type: ignore  # noqa: E402
from memory_runtime import remember_memory  # type: ignore  # noqa: E402
from proposal_manager import approve_memory_proposal, get_proposal, list_proposals  # type: ignore  # noqa: E402
from retrieve_memories import retrieve  # type: ignore  # noqa: E402

from .runtime import after as runtime_after
from .turn_service import complete_late_turn, get_turn, touch_turn


MAX_BODY_BYTES = 2_000_000
MAX_DIRECT_ASSET_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_ASSET_BYTES = 64 * 1024 * 1024
DEFAULT_ASSET_CHUNK_BYTES = 2 * 1024 * 1024
_TURN_PATH = re.compile(r"^/v1/turns/([^/]+)/(after|touch)$")
_PROPOSAL_APPROVE_PATH = re.compile(r"^/v1/proposals/([^/]+)/approve$")
_ASSET_PATH = re.compile(r"^/v1/assets/([^/]+)$")
_MAP_PATH = re.compile(r"^/v1/maps/([^/]+)$")
_OBSERVATION_PATH = re.compile(r"^/v1/spatial-observations/([^/]+)$")
_UPLOAD_PART_PATH = re.compile(r"^/v1/assets/uploads/([^/]+)/parts/(\d+)$")
_UPLOAD_COMPLETE_PATH = re.compile(r"^/v1/assets/uploads/([^/]+)/complete$")
_STATUS_PATHS = frozenset({"/v1/agent/status", "/v1/agents/status"})
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ACCESS_LOG_ENV = "META_MEMORY_HTTP_ACCESS_LOG"
SHUTDOWN_TIMEOUT_ENV = "META_MEMORY_HTTP_SHUTDOWN_TIMEOUT"
DEFAULT_SHUTDOWN_TIMEOUT = 10.0


@dataclass(frozen=True)
class Principal:
    """Identity and data bounds attached to one bearer token.

    Empty ``subject_ids`` and ``audiences`` retain the behaviour of 2.7-era
    agent files: they add no restriction beyond profile and workspace.  Once
    populated, every requested subject/audience must be in the allow-list.
    """

    profile_id: str
    agent_id: str
    workspaces: frozenset[str]
    permissions: frozenset[str]
    subject_ids: frozenset[str] = frozenset()
    audiences: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RequestIdentity:
    profile_id: str
    workspace_id: str
    subject_id: str
    agent_id: str
    session_id: str = ""
    audience_id: str = ""
    channel_id: str = ""


@dataclass(frozen=True)
class _RemoteWorkspaceContext:
    """ProjectContext-compatible object backed only by client workspace id."""

    name: str
    project_id: str
    root: Path
    workspace_id: str
    remote_identity: str = ""
    repository_fingerprint: str = ""


class _BoundConfig:
    """A request-scoped AppConfig view with token-bound identity."""

    def __init__(self, base: AppConfig, *, profile_id: str, subject_id: str) -> None:
        self._base = base
        self._profile_id = profile_id
        self._subject_id = subject_id

    @property
    def profile_id(self) -> str:
        return self._profile_id

    @property
    def subject_id(self) -> str:
        return self._subject_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


class ResourceNotFound(ValueError):
    pass


def _strings(value: object) -> frozenset[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(str(item).strip() for item in value if str(item).strip())


def load_principals(path: Path) -> dict[str, Principal]:
    """Load bearer principals while keeping the original agents.json valid."""

    raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    values = raw.get("agents") if isinstance(raw, dict) else None
    if not isinstance(values, dict):
        raise ValueError("agents file must contain an agents object")
    principals: dict[str, Principal] = {}
    for name, item in values.items():
        if not isinstance(item, dict):
            continue
        env_name = str(item.get("token_env") or "").strip()
        token = os.environ.get(env_name, "") if env_name else ""
        if not token:
            continue
        profile_id = str(item.get("profile_id") or "").strip()
        agent_id = str(item.get("agent_id") or name).strip()
        workspaces = _strings(item.get("workspaces", []))
        if not profile_id or not agent_id or not workspaces:
            raise ValueError(f"Agent {name!r} requires profile_id, agent_id and at least one workspace.")
        if token in principals:
            raise ValueError("Multiple agents resolve to the same bearer token.")
        principals[token] = Principal(
            profile_id=profile_id,
            agent_id=agent_id,
            workspaces=workspaces,
            permissions=frozenset(value.casefold() for value in _strings(item.get("permissions", []))),
            subject_ids=_strings(item.get("subject_ids", item.get("subjects", []))),
            audiences=_strings(item.get("audiences", item.get("audience_ids", []))),
        )
    if not principals:
        raise ValueError("No usable agent token found; set token_env values before starting the API.")
    return principals


def _permission_allowed(principal: Principal, permission: str) -> bool:
    permissions = principal.permissions
    requested = permission.casefold()
    if "*" in permissions or requested in permissions:
        return True
    # Old configurations predate explicit remote lifecycle/status names.
    if requested == "turns":
        return {"read", "record"}.issubset(permissions)
    if requested == "status":
        return bool(permissions.intersection({"read", "turns"}))
    if requested == "shared":
        return bool(permissions.intersection({"read", "record", "remember"}))
    if requested in {"assets", "maps", "spatial"}:
        return bool(permissions.intersection({"record", "remember", "shared"}))
    return False


def _bearer_token(header: str) -> str:
    scheme, separator, token = str(header or "").partition(" ")
    return token.strip() if separator and scheme.casefold() == "bearer" else ""


def _secret_equal(left: object, right: object) -> bool:
    return hmac.compare_digest(
        str(left).encode("utf-8"),
        str(right).encode("utf-8"),
    )


def authorize(
    tokens: Mapping[str, Principal],
    header: str,
    *,
    permission: str,
    workspace_id: str,
) -> Principal:
    """Authenticate a token and enforce permission plus workspace bounds."""

    token = _bearer_token(header)
    principal = next(
        (value for candidate, value in tokens.items() if token and _secret_equal(candidate, token)),
        None,
    )
    if principal is None:
        raise PermissionError("invalid bearer token")
    if not _permission_allowed(principal, permission):
        raise PermissionError(f"agent lacks {permission} permission")
    workspace = str(workspace_id or "").strip()
    if not workspace:
        raise ValueError("workspace_id is required")
    if "*" not in principal.workspaces and workspace not in principal.workspaces:
        raise PermissionError("workspace is not allowed for this agent")
    return principal


def identity(payload: Mapping[str, Any], principal: Principal) -> tuple[str, str, str]:
    """Return the legacy three-part identity after checking token binding."""

    requested_profile = str(payload.get("profile_id") or principal.profile_id).strip()
    requested_agent = str(payload.get("agent_id") or principal.agent_id).strip()
    workspace = str(payload.get("workspace_id") or "").strip()
    if requested_profile != principal.profile_id or requested_agent != principal.agent_id:
        raise PermissionError("request identity conflicts with token binding")
    if not workspace:
        raise ValueError("workspace_id is required")
    return requested_profile, workspace, requested_agent


def _requested_subjects(payload: Mapping[str, Any]) -> frozenset[str]:
    values = {
        str(payload.get("subject_id") or "").strip(),
        str(payload.get("principal_subject_id") or "").strip(),
        str(payload.get("state_subject_id") or "").strip(),
    }
    active = payload.get("active_subject_ids", [])
    if isinstance(active, (list, tuple, set, frozenset)):
        values.update(str(value).strip() for value in active)
    return frozenset(value for value in values if value)


def _requested_audiences(payload: Mapping[str, Any]) -> frozenset[str]:
    values = {
        str(payload.get("audience_id") or "").strip(),
    }
    many = payload.get("audience_ids", [])
    if isinstance(many, (list, tuple, set, frozenset)):
        values.update(str(value).strip() for value in many)
    return frozenset(value for value in values if value)


def _enforce_optional_bounds(payload: Mapping[str, Any], principal: Principal) -> None:
    subjects = _requested_subjects(payload)
    if principal.subject_ids and any(value not in principal.subject_ids for value in subjects):
        raise PermissionError("subject is not allowed for this agent")
    audiences = _requested_audiences(payload)
    if principal.audiences and any(value not in principal.audiences for value in audiences):
        raise PermissionError("audience or channel is not allowed for this agent")


def _request_identity(
    payload: Mapping[str, Any],
    principal: Principal,
    *,
    require_subject: bool,
    require_session: bool = False,
) -> RequestIdentity:
    profile_id, workspace_id, agent_id = identity(payload, principal)
    _enforce_optional_bounds(payload, principal)
    subject_id = str(payload.get("subject_id") or payload.get("principal_subject_id") or "").strip()
    if not subject_id and len(principal.subject_ids) == 1:
        subject_id = next(iter(principal.subject_ids))
    session_id = str(payload.get("session_id") or "").strip()
    if require_subject and not subject_id:
        raise ValueError("subject_id is required")
    if require_session and not session_id:
        raise ValueError("session_id is required")
    return RequestIdentity(
        profile_id=profile_id,
        workspace_id=workspace_id,
        subject_id=subject_id,
        agent_id=agent_id,
        session_id=session_id,
        audience_id=str(payload.get("audience_id") or "").strip(),
        channel_id=str(payload.get("channel_id") or "").strip(),
    )


def retrieval_args(
    root: Path,
    payload: Mapping[str, Any],
    profile_id: str,
    workspace_id: str,
    agent_id: str,
) -> argparse.Namespace:
    principal_subject = str(payload.get("principal_subject_id") or payload.get("subject_id") or "").strip()
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


def remember_args(
    root: Path,
    payload: Mapping[str, Any],
    profile_id: str,
    workspace_id: str,
    agent_id: str,
) -> argparse.Namespace:
    content = str(payload.get("content") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not content or not title:
        raise ValueError("title and content are required")
    return argparse.Namespace(
        store=str(root), subject_id=str(payload.get("subject_id") or ""), subject_name=str(payload.get("subject_name") or "Unknown"),
        session_id=str(payload.get("session_id") or ""), profile_id=profile_id, workspace_id=workspace_id, agent_id=agent_id,
        visibility_scope=str(payload.get("visibility_scope") or "workspace"), shared_mode=True, title=title, title_file=None,
        content=content, content_file=None, payload_file=None, force_kind=payload.get("force_kind"),
        use_underlying_kind=bool(payload.get("use_underlying_kind", False)), domain=payload.get("domain"),
        topic=payload.get("topic"), source=None, start_at=payload.get("start_at"), end_at=payload.get("end_at"),
        confidence=payload.get("confidence"), importance=payload.get("importance"), status=None,
        tag=list(payload.get("tags") or []), related_person=list(payload.get("related_people") or []),
        related_event=list(payload.get("related_events") or []), related_topic=list(payload.get("related_topics") or []),
        related_source=list(payload.get("related_sources") or []), slug=None, mode="create",
        topic_hint=str(payload.get("topic_hint") or ""), domain_hint=str(payload.get("domain_hint") or ""),
        source_ref=str(payload.get("source_ref") or ""), event_time=str(payload.get("event_time") or ""),
        skip_raw_record=False, allow_duplicate=bool(payload.get("allow_duplicate", False)), skip_index=False, out_file=None,
    )


def _bound_config(server: "APIServer", request: RequestIdentity) -> _BoundConfig:
    return _BoundConfig(server.config, profile_id=request.profile_id, subject_id=request.subject_id)


def _workspace_context(server: "APIServer", request: RequestIdentity) -> _RemoteWorkspaceContext:
    fingerprint = hashlib.sha256(
        f"{request.profile_id}\0{request.workspace_id}".encode("utf-8")
    ).hexdigest()[:16]
    # A deterministic synthetic path is used only by runtime audit/session
    # metadata.  It is never inferred from cwd and need not exist on disk.
    root = server.root / ".remote-workspaces" / fingerprint
    return _RemoteWorkspaceContext(
        name=request.workspace_id,
        project_id=request.workspace_id,
        root=root,
        workspace_id=request.workspace_id,
        repository_fingerprint=fingerprint,
    )


def _turn_scope(server: "APIServer", request: RequestIdentity, turn_uid: str) -> dict[str, str]:
    conn = open_db(server.root)
    try:
        row = conn.execute(
            "SELECT profile_id,workspace_id,subject_id,origin_agent_id,external_session_id,status "
            "FROM turns WHERE turn_uid=?",
            (turn_uid,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise ResourceNotFound("Turn not found.")
    actual = tuple(str(value or "") for value in row[:5])
    expected = (
        request.profile_id,
        request.workspace_id,
        request.subject_id,
        request.agent_id,
        request.session_id,
    )
    if actual != expected:
        raise PermissionError("turn is outside this token and request scope")
    return {"status": str(row[5] or ""), "turn_id": turn_uid}


def _channel_in_scope(
    server: "APIServer",
    principal: Principal,
    request: RequestIdentity,
    channel_id: str,
    *,
    require_exists: bool = True,
) -> dict[str, str]:
    channel = str(channel_id or "").strip()
    if not channel:
        raise ValueError("channel_id is required")
    explicitly_allowed = not principal.audiences or "*" in principal.audiences or channel in principal.audiences
    audience_allowed = bool(request.audience_id and request.audience_id in principal.audiences)
    if principal.audiences and not explicitly_allowed and not audience_allowed:
        raise PermissionError("channel audience is not allowed for this agent")
    conn = open_db(server.root)
    try:
        row = conn.execute(
            "SELECT channel_id,audience_id,profile_id FROM memory_channels "
            "WHERE channel_id=? AND status='active'",
            (channel,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        if principal.audiences and not explicitly_allowed:
            raise PermissionError("channel is not explicitly allowed and has no registered audience")
        if require_exists:
            raise ResourceNotFound("Channel not found.")
        return {"channel_id": channel, "audience_id": request.audience_id, "exists": "false"}
    if str(row[2] or "") != request.profile_id:
        raise PermissionError("channel is outside this token profile")
    audience = str(row[1] or "")
    if request.audience_id and request.audience_id != audience:
        raise PermissionError("channel does not belong to the requested audience")
    if principal.audiences and "*" not in principal.audiences:
        if channel not in principal.audiences and audience not in principal.audiences:
            raise PermissionError("channel audience is not allowed for this agent")
    return {"channel_id": channel, "audience_id": audience, "exists": "true"}


def _claim_in_scope(
    server: "APIServer",
    request: RequestIdentity,
    claim_id: str,
    *,
    allowed_subjects: frozenset[str] = frozenset(),
) -> None:
    conn = open_db(server.root)
    try:
        row = conn.execute(
            "SELECT profile_id,workspace_id,subject_id,visibility_scope,owner_agent_id FROM claims WHERE id=?",
            (claim_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise ResourceNotFound("Claim not found.")
    if str(row[0] or "") != request.profile_id or str(row[1] or "") != request.workspace_id:
        raise PermissionError("claim is outside this token scope")
    if request.subject_id and str(row[2] or "") != request.subject_id:
        raise PermissionError("claim subject is outside this request scope")
    if not request.subject_id and allowed_subjects and str(row[2] or "") not in allowed_subjects:
        raise PermissionError("claim subject is outside this token scope")
    if str(row[3] or "") == "agent" and str(row[4] or "") != request.agent_id:
        raise PermissionError("agent-private claim belongs to another Agent")


def _proposal_in_scope(
    server: "APIServer",
    request: RequestIdentity,
    proposal: Mapping[str, Any],
    *,
    allowed_subjects: frozenset[str] = frozenset(),
) -> bool:
    plan = proposal.get("plan", {})
    if not isinstance(plan, dict):
        return False
    profile_id = str(plan.get("profile_id") or "default")
    workspace_id = str(plan.get("workspace_id") or "global")
    subject_id = str(plan.get("subject_id") or proposal.get("subject_id") or "")
    return (
        profile_id == request.profile_id
        and workspace_id == request.workspace_id
        and (not request.subject_id or subject_id == request.subject_id)
        and (not allowed_subjects or subject_id in allowed_subjects)
    )


def _agent_runtime_status(server: "APIServer", request: RequestIdentity) -> dict[str, Any]:
    moment = datetime.now(timezone.utc).isoformat()
    conn = open_db(server.root)
    try:
        row = conn.execute(
            """
            SELECT last_before_at,last_after_at,last_write_at,last_retrieval_at,last_turn_uid,last_session_id,
                   last_retrieval_count,last_retrieval_duration_ms,total_before,total_after,total_degraded,
                   last_error_at,last_error_code,last_error_message,updated_at
            FROM agent_runtime_state WHERE profile_id=? AND agent_id=? AND workspace_id=?
            """,
            (request.profile_id, request.agent_id, request.workspace_id),
        ).fetchone()
        turn_counts = conn.execute(
            """
            SELECT status,COUNT(*) FROM turns
            WHERE profile_id=? AND origin_agent_id=? AND workspace_id=?
            GROUP BY status
            """,
            (request.profile_id, request.agent_id, request.workspace_id),
        ).fetchall()
        world_counts = {
            "shared_activities": int(conn.execute(
                "SELECT COUNT(*) FROM shared_activities WHERE profile_id=? AND status='active' "
                "AND (valid_until IS NULL OR valid_until>?)",
                (request.profile_id, moment),
            ).fetchone()[0]),
            "current_states": int(conn.execute(
                "SELECT COUNT(*) FROM temporal_states WHERE profile_id=? AND status='active' "
                "AND valid_from<=? AND (valid_until IS NULL OR valid_until>?)",
                (request.profile_id, moment, moment),
            ).fetchone()[0]),
            "assets": int(conn.execute(
                "SELECT COUNT(*) FROM binary_assets WHERE profile_id=? AND status='active'",
                (request.profile_id,),
            ).fetchone()[0]),
            "maps": int(conn.execute(
                "SELECT COUNT(DISTINCT map_id) FROM spatial_maps WHERE profile_id=? AND status='active'",
                (request.profile_id,),
            ).fetchone()[0]),
            "spatial_observations": int(conn.execute(
                "SELECT COUNT(*) FROM spatial_observations WHERE profile_id=? AND status='active' "
                "AND (valid_until IS NULL OR valid_until>?)",
                (request.profile_id, moment),
            ).fetchone()[0]),
        }
    finally:
        conn.close()
    counts = {str(status): int(count) for status, count in turn_counts}
    if not row:
        lifecycle = "never_seen"
        runtime: dict[str, Any] = {
            "last_before_at": "", "last_after_at": "", "last_write_at": "", "last_retrieval_at": "",
            "last_turn_id": "", "last_session_id": "", "last_retrieval_count": 0,
            "last_retrieval_duration_ms": 0, "total_before": 0, "total_after": 0,
            "total_degraded": 0, "last_error_at": "", "last_error_code": "",
            "last_error_message": "", "updated_at": "",
        }
    else:
        runtime = {
            "last_before_at": str(row[0] or ""), "last_after_at": str(row[1] or ""),
            "last_write_at": str(row[2] or ""), "last_retrieval_at": str(row[3] or ""),
            "last_turn_id": str(row[4] or ""), "last_session_id": str(row[5] or ""),
            "last_retrieval_count": int(row[6] or 0), "last_retrieval_duration_ms": int(row[7] or 0),
            "total_before": int(row[8] or 0), "total_after": int(row[9] or 0),
            "total_degraded": int(row[10] or 0), "last_error_at": str(row[11] or ""),
            "last_error_code": str(row[12] or ""), "last_error_message": str(row[13] or ""),
            "updated_at": str(row[14] or ""),
        }
        if runtime["last_error_at"] and runtime["last_error_at"] >= max(
            runtime["last_before_at"], runtime["last_after_at"]
        ):
            lifecycle = "error"
        elif runtime["total_before"] and runtime["total_after"]:
            lifecycle = "active"
        elif runtime["total_before"]:
            lifecycle = "before_only"
        else:
            lifecycle = "never_seen"
    return {
        "status": "ok",
        "agent": {
            "profile_id": request.profile_id,
            "agent_id": request.agent_id,
            "workspace_id": request.workspace_id,
            "subject_id": request.subject_id,
            "audience_id": request.audience_id,
            "channel_id": request.channel_id,
            "lifecycle_state": lifecycle,
            "active": lifecycle == "active",
            "last_before": runtime["last_before_at"],
            "last_after": runtime["last_after_at"],
            "turn_counts": counts,
            "shared_world_counts": world_counts,
            "pending_turns": int(counts.get("started", 0)),
            **runtime,
        },
    }


def _upload_root(server: "APIServer") -> Path:
    root = (server.root / "assets" / "uploads").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _upload_directory(server: "APIServer", upload_id: str) -> Path:
    identifier = str(upload_id or "").strip()
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise ValueError("invalid upload_id")
    target = (_upload_root(server) / identifier).resolve()
    if not target.is_relative_to(_upload_root(server)):
        raise ValueError("invalid upload path")
    return target


def _atomic_json_file(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_upload(server: "APIServer", upload_id: str) -> tuple[Path, dict[str, Any]]:
    directory = _upload_directory(server, upload_id)
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise ResourceNotFound("Upload not found.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("upload manifest is unreadable") from exc
    if not isinstance(manifest, dict) or str(manifest.get("upload_id") or "") != upload_id:
        raise ValueError("upload manifest is invalid")
    return directory, manifest


def _upload_identity_matches(manifest: Mapping[str, Any], request: RequestIdentity) -> None:
    expected = (
        request.profile_id,
        request.agent_id,
        request.workspace_id,
        request.subject_id,
    )
    actual = tuple(
        str(manifest.get(key) or "")
        for key in ("profile_id", "agent_id", "workspace_id", "subject_id")
    )
    if actual != expected:
        raise PermissionError("upload belongs to another token identity")


class _PartsReader:
    """Small sequential stream over already verified upload chunks."""

    def __init__(self, paths: list[Path]) -> None:
        self.paths = paths
        self.index = 0
        self.current = None

    def read(self, size: int = -1) -> bytes:
        wanted = 1024 * 1024 if size is None or size < 0 else max(1, int(size))
        output = bytearray()
        while len(output) < wanted and self.index < len(self.paths):
            if self.current is None:
                self.current = self.paths[self.index].open("rb")
            chunk = self.current.read(wanted - len(output))
            if chunk:
                output.extend(chunk)
                continue
            self.current.close()
            self.current = None
            self.index += 1
        return bytes(output)

    def close(self) -> None:
        if self.current is not None:
            self.current.close()
            self.current = None


def _empty_shared_context(request: RequestIdentity) -> dict[str, Any]:
    return {
        "profile_id": request.profile_id,
        "channel_id": request.channel_id,
        "activities": [],
        "states": [],
        "spatial": [],
        "counts": {"activities": 0, "states": 0, "spatial": 0},
        "truncated": False,
    }


def _environment_enabled(name: str, *, default: bool) -> bool:
    value = str(os.environ.get(name, "")).strip().casefold()
    if not value:
        return default
    return value not in {"0", "false", "no", "off", "disabled"}


def _shutdown_timeout() -> float:
    value = str(os.environ.get(SHUTDOWN_TIMEOUT_ENV, "")).strip()
    if not value:
        return DEFAULT_SHUTDOWN_TIMEOUT
    try:
        parsed = float(value)
    except ValueError:
        return DEFAULT_SHUTDOWN_TIMEOUT
    return max(0.0, min(parsed, 300.0))


def _writable_directory_probe(directory: Path) -> dict[str, Any]:
    """Prove a directory can persist bytes without leaving a probe artifact."""

    directory.mkdir(parents=True, exist_ok=True)
    # TemporaryFile is anonymous where the platform supports O_TMPFILE and is
    # delete-on-close elsewhere.  In either case readiness polling never
    # accumulates sentinel files in a persisted Docker volume.
    with tempfile.TemporaryFile(
        mode="w+b", prefix=".meta-memory-ready-", dir=directory
    ) as probe:
        marker = uuid.uuid4().bytes
        probe.write(marker)
        probe.flush()
        probe.seek(0)
        if probe.read() != marker:
            raise OSError("readiness write probe could not be read back")
    return {"status": "ok", "writable": True}


def _readiness(server: "APIServer") -> tuple[HTTPStatus, dict[str, Any]]:
    """Check the loaded bindings, current schema and persistent directories."""

    checks: dict[str, dict[str, Any]] = {}
    bindings_valid = bool(server.tokens) and all(
        bool(str(token))
        and isinstance(principal, Principal)
        and bool(principal.profile_id)
        and bool(principal.agent_id)
        and bool(principal.workspaces)
        for token, principal in server.tokens.items()
    )
    checks["agent_bindings"] = {
        "status": "ok" if bindings_valid else "error",
        "loaded": bindings_valid,
        "count": len(server.tokens),
    }

    connection = None
    try:
        from db_migrations import checksum, migration_files  # type: ignore

        packaged = migration_files()
        expected = [path.name.split("_", 1)[0] for path in packaged]
        expected_checksums = {
            path.name.split("_", 1)[0]: checksum(path) for path in packaged
        }
        connection = open_db(server.root)
        connection.execute("SELECT 1").fetchone()
        applied_rows = [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT version,checksum FROM schema_migrations ORDER BY version"
            )
        ]
        applied = [version for version, _digest in applied_rows]
        if applied != expected:
            raise RuntimeError("database migrations are incomplete")
        if any(
            not hmac.compare_digest(digest, expected_checksums.get(version, ""))
            for version, digest in applied_rows
        ):
            raise RuntimeError("database migration checksums do not match this release")
        checks["database"] = {
            "status": "ok",
            "accessible": True,
            "schema_current": True,
            "checksums_valid": True,
            "schema_version": expected[-1] if expected else "",
        }
    except Exception as exc:
        checks["database"] = {
            "status": "error",
            "accessible": False,
            "schema_current": False,
            "checksums_valid": False,
            "error_type": type(exc).__name__,
        }
    finally:
        if connection is not None:
            connection.close()

    storage_groups = {
        "store": (
            ("root_writable", server.root),
            ("database_directory_writable", server.root / "db"),
        ),
        "assets": (
            ("objects_writable", server.root / "assets" / "objects"),
            ("uploads_writable", server.root / "assets" / "uploads"),
        ),
    }
    for name, probes in storage_groups.items():
        details: dict[str, bool] = {}
        label = ""
        try:
            for label, directory in probes:
                details[label] = bool(
                    _writable_directory_probe(directory)["writable"]
                )
            checks[name] = {"status": "ok", "writable": True, **details}
        except Exception as exc:
            checks[name] = {
                "status": "error",
                "writable": False,
                "error_type": type(exc).__name__,
                "failed_probe": label,
                **details,
            }

    ready = all(check.get("status") == "ok" for check in checks.values())
    body: dict[str, Any] = {
        "status": "ready" if ready else "not_ready",
        "checks": checks,
    }
    return (HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE), body


class MemoryAPI(BaseHTTPRequestHandler):
    server: "APIServer"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def handle_one_request(self) -> None:
        self._request_id = uuid.uuid4().hex
        self._response_status = 0
        self._request_error_type = ""
        started = time.monotonic()
        try:
            super().handle_one_request()
        finally:
            elapsed_ms = round((time.monotonic() - started) * 1000, 3)
            method = str(getattr(self, "command", "") or "")
            raw_path = str(getattr(self, "path", "") or "")
            try:
                path = urlsplit(raw_path).path
            except ValueError:
                path = ""
            if method or self._response_status:
                self.server.log_request_event(
                    request_id=self._request_id,
                    method=method or "UNKNOWN",
                    path=path or "/",
                    status=self._response_status,
                    duration_ms=elapsed_ms,
                    error_type=self._request_error_type,
                )

    def parse_request(self) -> bool:
        parsed = super().parse_request()
        if parsed:
            supplied = str(self.headers.get("X-Request-ID") or "").strip()
            matches_token = any(
                _secret_equal(supplied, token)
                for token in self.server.tokens
                if supplied
            )
            if _REQUEST_ID.fullmatch(supplied) and not matches_token:
                self._request_id = supplied
        return parsed

    def send_response(self, code: int, message: str | None = None) -> None:
        self._response_status = int(code)
        super().send_response(code, message)

    def end_headers(self) -> None:
        self.send_header("X-Request-ID", self._request_id)
        super().end_headers()

    def _record_unhandled(self, error: BaseException) -> None:
        # Only the exception class reaches access logs.  Exception messages can
        # contain request data, filesystem names or other operator secrets.
        self._request_error_type = type(error).__name__

    def _json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("request body is required and must be below 2 MB")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON object required")
        return value

    def _raw_body(self, *, maximum: int) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0 or length > int(maximum):
            raise ValueError(f"request body is required and must be below {int(maximum)} bytes")
        value = self.rfile.read(length)
        if len(value) != length:
            raise ValueError("request body ended before Content-Length")
        return value

    def _reply(self, status: HTTPStatus, body: Mapping[str, Any]) -> None:
        encoded = json.dumps(dict(body), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _reply_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        *,
        media_type: str,
        filename: str = "",
        sha256: str = "",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", media_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        if filename:
            clean = str(filename).replace('"', "").replace("\r", "").replace("\n", "")
            fallback = clean.encode("ascii", "ignore").decode("ascii").strip() or "download"
            encoded_name = quote(clean, safe="")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded_name}',
            )
        if sha256:
            self.send_header("X-Content-SHA256", sha256)
        self.end_headers()
        self.wfile.write(body)

    def _reply_file(
        self,
        status: HTTPStatus,
        path: Path,
        *,
        media_type: str,
        filename: str = "",
        sha256: str = "",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", media_type or "application/octet-stream")
        self.send_header("Content-Length", str(path.stat().st_size))
        if filename:
            clean = str(filename).replace('"', "").replace("\r", "").replace("\n", "")
            fallback = clean.encode("ascii", "ignore").decode("ascii").strip() or "download"
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(clean, safe="")}',
            )
        if sha256:
            self.send_header("X-Content-SHA256", sha256)
        self.end_headers()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                self.wfile.write(block)

    def _principal(self, payload: Mapping[str, Any], *, permission: str) -> Principal:
        return authorize(
            self.server.tokens,
            self.headers.get("Authorization", ""),
            permission=permission,
            workspace_id=str(payload.get("workspace_id") or ""),
        )

    def _turn_identity(self, payload: Mapping[str, Any], *, permission: str = "turns") -> RequestIdentity:
        principal = self._principal(payload, permission=permission)
        return _request_identity(payload, principal, require_subject=True, require_session=True)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/healthz":
                self._reply(HTTPStatus.OK, {"status": "ok"})
                return
            if parsed.path == "/readyz":
                status, body = _readiness(self.server)
                self._reply(status, body)
                return
            if parsed.path in _STATUS_PATHS:
                query = {key: values[-1] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
                principal = self._principal(query, permission="status")
                request = _request_identity(query, principal, require_subject=False)
                if request.channel_id:
                    _channel_in_scope(self.server, principal, request, request.channel_id, require_exists=False)
                self._reply(HTTPStatus.OK, _agent_runtime_status(self.server, request))
                return
            query = {key: values[-1] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
            asset_match = _ASSET_PATH.fullmatch(parsed.path.rstrip("/"))
            if asset_match:
                principal = self._principal(query, permission="assets")
                request = _request_identity(query, principal, require_subject=False)
                from .spatial import asset_file, get_asset

                asset_id = unquote(asset_match.group(1)).strip()
                if request.channel_id:
                    _channel_in_scope(self.server, principal, request, request.channel_id)
                asset = get_asset(
                    self.server.root,
                    profile_id=request.profile_id,
                    asset_id=asset_id,
                    enforce_visibility=True,
                    channel_id=request.channel_id,
                    workspace_id=request.workspace_id,
                    viewer_agent_id=request.agent_id,
                )
                if not asset:
                    raise ResourceNotFound("Asset not found.")
                if str(query.get("download") or "").casefold() in {"1", "true", "yes"}:
                    content_path = asset_file(
                        self.server.root,
                        profile_id=request.profile_id,
                        asset_id=asset_id,
                        max_bytes=self.server.max_asset_bytes,
                    )
                    self._reply_file(
                        HTTPStatus.OK,
                        content_path,
                        media_type=str(asset.get("media_type") or "application/octet-stream"),
                        filename=str(asset.get("original_name") or ""),
                        sha256=str(asset.get("sha256") or ""),
                    )
                else:
                    self._reply(HTTPStatus.OK, {"status": "ok", "asset": asset})
                return
            map_match = _MAP_PATH.fullmatch(parsed.path.rstrip("/"))
            if map_match:
                principal = self._principal(query, permission="maps")
                request = _request_identity(query, principal, require_subject=False)
                from .spatial import get_map

                version_text = str(query.get("version") or "").strip()
                item = get_map(
                    self.server.root,
                    profile_id=request.profile_id,
                    map_id=unquote(map_match.group(1)).strip(),
                    version=int(version_text) if version_text else None,
                )
                if not item:
                    raise ResourceNotFound("Map not found.")
                _channel_in_scope(self.server, principal, request, str(item.get("channel_id") or ""))
                self._reply(HTTPStatus.OK, {"status": "ok", "map": item})
                return
            observation_match = _OBSERVATION_PATH.fullmatch(parsed.path.rstrip("/"))
            if observation_match:
                principal = self._principal(query, permission="spatial")
                request = _request_identity(query, principal, require_subject=False)
                from .spatial import get_spatial_observation

                observation_id = unquote(observation_match.group(1)).strip()
                raw = get_spatial_observation(
                    self.server.root,
                    profile_id=request.profile_id,
                    observation_id=observation_id,
                    include_history=True,
                )
                if not raw:
                    raise ResourceNotFound("Spatial observation not found.")
                channel_id = str(raw.get("channel_id") or "")
                _channel_in_scope(self.server, principal, request, channel_id)
                item = get_spatial_observation(
                    self.server.root,
                    profile_id=request.profile_id,
                    observation_id=observation_id,
                    include_history=True,
                    channel_id=channel_id,
                    subject_id=request.subject_id,
                    subject_ids=principal.subject_ids if not request.subject_id else None,
                    workspace_id=request.workspace_id,
                    viewer_agent_id=request.agent_id,
                    viewer_subject_ids=(
                        principal.subject_ids
                        if principal.subject_ids
                        else ({request.subject_id} if request.subject_id else None)
                    ),
                )
                if not item:
                    raise ResourceNotFound("Spatial observation not found.")
                self._reply(HTTPStatus.OK, {"status": "ok", "observation": item})
                return
            if parsed.path in {"/v1/channels", "/v1/activities", "/v1/states"}:
                principal = self._principal(query, permission="shared")
                request = _request_identity(query, principal, require_subject=False)
                from .shared_memory import list_activity_feed, list_channels, list_temporal_states

                channel_id = str(query.get("channel_id") or "").strip()
                if channel_id:
                    _channel_in_scope(self.server, principal, request, channel_id)
                elif principal.audiences and parsed.path != "/v1/channels":
                    raise ValueError("channel_id is required for an audience-bounded token")
                if parsed.path == "/v1/channels":
                    rows = list_channels(
                        self.server.root,
                        profile_id=request.profile_id,
                        audience_id=str(query.get("audience_id") or ""),
                        member_type="agent",
                        member_id=request.agent_id,
                    )
                    if principal.audiences and "*" not in principal.audiences:
                        rows = [
                            row for row in rows
                            if str(row.get("channel_id") or "") in principal.audiences
                            or str(row.get("audience_id") or "") in principal.audiences
                        ]
                    self._reply(HTTPStatus.OK, {"status": "ok", "channels": rows})
                elif parsed.path == "/v1/activities":
                    rows = list_activity_feed(
                        self.server.root,
                        profile_id=request.profile_id,
                        channel_id=channel_id,
                        audience_id=str(query.get("audience_id") or ""),
                        member_type="agent",
                        member_id=request.agent_id,
                        subject_id=request.subject_id,
                        subject_ids=principal.subject_ids if not request.subject_id else None,
                        limit=int(query.get("limit") or 100),
                    )
                    self._reply(HTTPStatus.OK, {"status": "ok", "activities": rows})
                else:
                    rows = list_temporal_states(
                        self.server.root,
                        profile_id=request.profile_id,
                        channel_id=channel_id,
                        subject_id=request.subject_id,
                        subject_ids=principal.subject_ids if not request.subject_id else None,
                        state_key=str(query.get("state_key") or ""),
                        current_only=str(query.get("include_history") or "").casefold() not in {"1", "true", "yes"},
                        limit=int(query.get("limit") or 100),
                    )
                    self._reply(HTTPStatus.OK, {"status": "ok", "states": rows})
                return
            if parsed.path in {"/v1/assets", "/v1/maps", "/v1/spatial-observations"}:
                permission = "assets" if parsed.path == "/v1/assets" else "maps" if parsed.path == "/v1/maps" else "spatial"
                principal = self._principal(query, permission=permission)
                request = _request_identity(query, principal, require_subject=False)
                channel_id = str(query.get("channel_id") or "").strip()
                if channel_id:
                    _channel_in_scope(self.server, principal, request, channel_id)
                elif principal.audiences and parsed.path != "/v1/assets":
                    raise ValueError("channel_id is required for an audience-bounded token")
                if parsed.path == "/v1/assets":
                    from .spatial import list_assets

                    rows = list_assets(
                        self.server.root,
                        profile_id=request.profile_id,
                        media_type=str(query.get("media_type") or ""),
                        limit=int(query.get("limit") or 100),
                        enforce_visibility=True,
                        channel_id=request.channel_id,
                        workspace_id=request.workspace_id,
                        viewer_agent_id=request.agent_id,
                    )
                    self._reply(HTTPStatus.OK, {"status": "ok", "assets": rows})
                elif parsed.path == "/v1/maps":
                    from .spatial import list_maps

                    rows = list_maps(
                        self.server.root,
                        profile_id=request.profile_id,
                        channel_id=channel_id,
                        latest_only=str(query.get("include_history") or "").casefold() not in {"1", "true", "yes"},
                        limit=int(query.get("limit") or 100),
                    )
                    self._reply(HTTPStatus.OK, {"status": "ok", "maps": rows})
                else:
                    from .spatial import list_spatial_observations, search_spatial_observations

                    common = dict(
                        profile_id=request.profile_id,
                        channel_id=channel_id,
                        map_id=str(query.get("map_id") or ""),
                        subject_id=request.subject_id,
                        workspace_id=request.workspace_id,
                        viewer_agent_id=request.agent_id,
                        subject_ids=principal.subject_ids if not request.subject_id else None,
                        viewer_subject_ids=(
                            principal.subject_ids
                            if principal.subject_ids
                            else ({request.subject_id} if request.subject_id else None)
                        ),
                        current_only=str(query.get("include_history") or "").casefold() not in {"1", "true", "yes"},
                        limit=int(query.get("limit") or 100),
                    )
                    search = str(query.get("query") or "").strip()
                    rows = search_spatial_observations(self.server.root, query=search, **common) if search else list_spatial_observations(
                        self.server.root,
                        location_id=str(query.get("location_id") or ""),
                        **common,
                    )
                    self._reply(HTTPStatus.OK, {"status": "ok", "observations": rows})
                return
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except ResourceNotFound as exc:
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found", "detail": str(exc)})
        except PermissionError as exc:
            self._reply(HTTPStatus.FORBIDDEN, {"error": "forbidden", "detail": str(exc)})
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._reply(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "detail": str(exc)})
        except Exception as exc:
            self._record_unhandled(exc)
            self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        known = path in {
            "/v1/retrieve", "/v1/events", "/v1/remember", "/v1/feedback", "/v1/proposals/list",
            "/v1/turns/before", "/v1/recovery/replay", "/v1/channels", "/v1/activities",
            "/v1/states", "/v1/assets", "/v1/assets/uploads", "/v1/maps",
            "/v1/spatial-observations", *_STATUS_PATHS,
        } or bool(_TURN_PATH.fullmatch(path)) or bool(_PROPOSAL_APPROVE_PATH.fullmatch(path)) \
            or bool(_UPLOAD_COMPLETE_PATH.fullmatch(path))
        if not known:
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            content_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
            if path == "/v1/assets" and content_type != "application/json":
                self._reply(HTTPStatus.OK, self._direct_asset_upload(parsed))
                return
            payload = self._json_body()
            result = self._dispatch_post(path, payload)
            self._reply(HTTPStatus.OK, result)
        except ResourceNotFound as exc:
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found", "detail": str(exc)})
        except PermissionError as exc:
            self._reply(HTTPStatus.FORBIDDEN, {"error": "forbidden", "detail": str(exc)})
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._reply(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "detail": str(exc)})
        except Exception as exc:
            self._record_unhandled(exc)
            self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"})

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        match = _UPLOAD_PART_PATH.fullmatch(parsed.path.rstrip("/"))
        if not match:
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            query = {key: values[-1] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
            principal = self._principal(query, permission="assets")
            request = _request_identity(query, principal, require_subject=False)
            upload_id = unquote(match.group(1)).strip()
            index = int(match.group(2))
            directory, manifest = _load_upload(self.server, upload_id)
            _upload_identity_matches(manifest, request)
            data = self._raw_body(maximum=self.server.asset_chunk_bytes)
            digest = hashlib.sha256(data).hexdigest()
            expected = str(self.headers.get("X-Chunk-SHA256") or "").strip().casefold()
            if expected and not hmac.compare_digest(expected, digest):
                raise ValueError("chunk SHA-256 does not match")
            target = directory / f"{index:08d}.part"
            temporary = directory / f".{index:08d}.{uuid.uuid4().hex}.tmp"
            try:
                temporary.write_bytes(data)
                if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    raise ValueError("part index already contains different bytes")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            self._reply(
                HTTPStatus.OK,
                {"status": "ok", "upload_id": upload_id, "part": index, "byte_size": len(data), "sha256": digest},
            )
        except ResourceNotFound as exc:
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found", "detail": str(exc)})
        except PermissionError as exc:
            self._reply(HTTPStatus.FORBIDDEN, {"error": "forbidden", "detail": str(exc)})
        except (ValueError, KeyError, OSError) as exc:
            self._reply(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "detail": str(exc)})
        except Exception as exc:
            self._record_unhandled(exc)
            self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"})

    def _direct_asset_upload(self, parsed) -> dict[str, Any]:
        query = {key: values[-1] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
        principal = self._principal(query, permission="assets")
        request = _request_identity(query, principal, require_subject=False)
        if request.channel_id:
            _channel_in_scope(self.server, principal, request, request.channel_id)
        from .spatial import store_asset

        content = self._raw_body(maximum=min(self.server.max_asset_bytes, MAX_DIRECT_ASSET_BYTES))
        expected = str(self.headers.get("X-Content-SHA256") or "").strip().casefold()
        actual = hashlib.sha256(content).hexdigest()
        if expected and not hmac.compare_digest(expected, actual):
            raise ValueError("asset SHA-256 does not match")
        raw_name = str(self.headers.get("X-Meta-Memory-Original-Name") or "")
        result = store_asset(
            self.server.root,
            content,
            profile_id=request.profile_id,
            media_type=str(self.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0],
            original_name=unquote(raw_name),
            metadata={"source_agent_id": request.agent_id, "source_workspace_id": request.workspace_id},
            max_bytes=self.server.max_asset_bytes,
            visibility_scope="channel" if request.channel_id else "workspace",
            channel_id=request.channel_id,
            workspace_id=request.workspace_id,
            owner_agent_id=request.agent_id,
            source_subject_id=request.subject_id,
            source_agent_id=request.agent_id,
        )
        return {"status": "ok", "asset": result}

    def _dispatch_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path in _STATUS_PATHS:
            principal = self._principal(payload, permission="status")
            request = _request_identity(payload, principal, require_subject=False)
            if request.channel_id:
                _channel_in_scope(self.server, principal, request, request.channel_id, require_exists=False)
            return _agent_runtime_status(self.server, request)

        if path == "/v1/turns/before":
            principal = self._principal(payload, permission="turns")
            request = _request_identity(payload, principal, require_subject=True, require_session=True)
            query = str(payload.get("query") or "")
            turn_uid = str(payload.get("turn_id") or "").strip()
            if not query.strip():
                raise ValueError("query is required")
            if not turn_uid:
                raise ValueError("turn_id is required")
            channel_scope = None
            if request.channel_id:
                # Validate all requested sharing boundaries before begin_turn
                # persists either the Turn or the raw user message.
                channel_scope = _channel_in_scope(
                    self.server, principal, request, request.channel_id, require_exists=False
                )
            from .turn_service import begin_turn

            result = begin_turn(
                _bound_config(self.server, request),
                query=query,
                requested_session=request.session_id,
                agent_id=request.agent_id,
                requested_turn_uid=turn_uid,
                project_context=_workspace_context(self.server, request),  # type: ignore[arg-type]
            )
            result.pop("project_root", None)
            result.update(
                profile_id=request.profile_id,
                workspace_id=request.workspace_id,
                subject_id=request.subject_id,
                audience_id=request.audience_id,
                channel_id=request.channel_id,
            )
            result["shared_context"] = _empty_shared_context(request)
            if request.channel_id:
                if channel_scope and channel_scope.get("exists") == "true":
                    from .shared_memory import build_shared_context

                    result["shared_context"] = build_shared_context(
                        self.server.root,
                        profile_id=request.profile_id,
                        channel_id=request.channel_id,
                        subject_id="" if principal.subject_ids else request.subject_id,
                        subject_ids=principal.subject_ids or None,
                        agent_id=request.agent_id,
                        workspace_id=request.workspace_id,
                    )
            return result

        turn_match = _TURN_PATH.fullmatch(path)
        if turn_match:
            turn_uid = unquote(turn_match.group(1)).strip()
            action = turn_match.group(2)
            request = self._turn_identity(payload)
            payload_turn = str(payload.get("turn_id") or "").strip()
            if payload_turn and payload_turn != turn_uid:
                raise ValueError("payload turn_id conflicts with request path")
            assistant = str(payload.get("assistant") or payload.get("assistant_text") or "")
            if action != "touch":
                answer_sha256 = hashlib.sha256(assistant.encode("utf-8")).hexdigest()
                declared_hash = str(payload.get("answer_sha256") or "").strip().casefold()
                if declared_hash and not hmac.compare_digest(declared_hash, answer_sha256):
                    raise ValueError("answer_sha256 does not match assistant_text")
            # Reject an internally inconsistent response before opening
            # SQLite.  Besides being cheaper, this prevents a bad client
            # payload from waiting behind an unrelated writer for the full DB
            # busy timeout.  Authentication and fixed identity validation have
            # already run above; the actual Turn scope is still checked before
            # any touch or completion.
            _turn_scope(self.server, request, turn_uid)
            config = _bound_config(self.server, request)
            if action == "touch":
                return touch_turn(config, turn_uid=turn_uid, agent_id=request.agent_id, note=str(payload.get("note") or ""))
            return runtime_after(config, assistant_text=assistant, turn_uid=turn_uid, agent_id=request.agent_id)

        if path == "/v1/recovery/replay":
            principal = self._principal(payload, permission="turns")
            request = _request_identity(payload, principal, require_subject=True, require_session=False)
            turn_uid = str(payload.get("turn_id") or "").strip()
            assistant = str(payload.get("assistant") or payload.get("assistant_text") or "")
            answer_sha256 = hashlib.sha256(assistant.encode("utf-8")).hexdigest()
            declared_hash = str(payload.get("answer_sha256") or "").strip().casefold()
            if declared_hash and not hmac.compare_digest(declared_hash, answer_sha256):
                raise ValueError("answer_sha256 does not match assistant_text")
            config = _bound_config(self.server, request)
            if turn_uid or assistant.strip():
                if not turn_uid or not assistant.strip() or not request.session_id:
                    raise ValueError("turn_id, assistant, and session_id must be supplied together")
                state = _turn_scope(self.server, request, turn_uid)
                if state["status"] == "abandoned":
                    result = complete_late_turn(config, turn_uid=turn_uid, assistant_text=assistant, agent_id=request.agent_id)
                else:
                    result = runtime_after(config, assistant_text=assistant, turn_uid=turn_uid, agent_id=request.agent_id)
                return {**result, "replayed": True}
            from .turn_recovery import recover_expired_turns

            recovered = recover_expired_turns(
                config,
                limit=max(1, min(int(payload.get("limit") or 100), 500)),
                agent_id=request.agent_id,
                workspace_id=request.workspace_id,
            )
            return {"status": "ok", "turn_recovery": recovered, "scope": {"agent_id": request.agent_id, "workspace_id": request.workspace_id}}

        if path in {"/v1/channels", "/v1/activities", "/v1/states"}:
            principal = self._principal(payload, permission="shared")
            request = _request_identity(payload, principal, require_subject=False)
            from .shared_memory import (
                ensure_audience,
                ensure_channel,
                grant_audience_member,
                publish_activity,
                publish_temporal_state,
            )

            if path == "/v1/channels":
                audience = ensure_audience(
                    self.server.root,
                    profile_id=request.profile_id,
                    audience_type=str(payload.get("audience_type") or "household"),
                    audience_key=str(payload.get("audience_key") or payload.get("channel_key") or ""),
                    label=str(payload.get("label") or ""),
                    metadata=payload.get("audience_metadata") if isinstance(payload.get("audience_metadata"), dict) else None,
                    profile_wide=bool(payload.get("profile_wide", False)),
                )
                channel = ensure_channel(
                    self.server.root,
                    profile_id=request.profile_id,
                    channel_type=str(payload.get("channel_type") or payload.get("audience_type") or "household"),
                    channel_key=str(payload.get("channel_key") or payload.get("audience_key") or ""),
                    audience_id=str(audience.get("audience_id") or ""),
                    subject_id=request.subject_id,
                    workspace_id=request.workspace_id,
                    owner_agent_id=request.agent_id,
                    label=str(payload.get("label") or ""),
                    metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
                )
                grant_audience_member(
                    self.server.root,
                    profile_id=request.profile_id,
                    audience_id=str(audience.get("audience_id") or ""),
                    member_type="agent",
                    member_id=request.agent_id,
                )
                return {"status": "ok", "audience": audience, "channel": channel}
            channel_id = str(payload.get("channel_id") or "")
            _channel_in_scope(self.server, principal, request, channel_id)
            if path == "/v1/activities":
                item = publish_activity(
                    self.server.root,
                    profile_id=request.profile_id,
                    channel_id=channel_id,
                    summary=str(payload.get("summary") or payload.get("content") or ""),
                    source_workspace_id=request.workspace_id,
                    subject_id=request.subject_id,
                    source_agent_id=request.agent_id,
                    source_session_id=request.session_id,
                    source_ref=str(payload.get("source_ref") or ""),
                    confidence=payload.get("confidence"),
                    activity_kind=str(payload.get("activity_kind") or "update"),
                    title=str(payload.get("title") or ""),
                    payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else None,
                    importance=(
                        0.5 if payload.get("importance") is None
                        else float(payload.get("importance"))
                    ),
                    occurred_at=payload.get("occurred_at"),
                    valid_until=payload.get("valid_until"),
                    supersedes_activity_id=str(payload.get("supersedes_activity_id") or ""),
                    idempotency_key=str(payload.get("idempotency_key") or ""),
                )
                return {"status": "ok", "activity": item}
            item = publish_temporal_state(
                self.server.root,
                profile_id=request.profile_id,
                channel_id=channel_id,
                subject_id=request.subject_id or str(payload.get("state_subject_id") or ""),
                state_key=str(payload.get("state_key") or ""),
                value=payload.get("value", {"content": str(payload.get("content") or "")}),
                summary=str(payload.get("summary") or payload.get("content") or ""),
                source_workspace_id=request.workspace_id,
                source_agent_id=request.agent_id,
                source_ref=str(payload.get("source_ref") or ""),
                confidence=payload.get("confidence"),
                observed_at=payload.get("observed_at"),
                valid_from=payload.get("valid_from"),
                valid_until=payload.get("valid_until"),
                supersedes_state_id=str(payload.get("supersedes_state_id") or ""),
                idempotency_key=str(payload.get("idempotency_key") or ""),
                metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
            )
            return {"status": "ok", "state": item}

        if path == "/v1/assets/uploads":
            principal = self._principal(payload, permission="assets")
            request = _request_identity(payload, principal, require_subject=False)
            if request.channel_id:
                _channel_in_scope(self.server, principal, request, request.channel_id)
            declared_size = int(payload.get("byte_size") or 0)
            if declared_size <= 0 or declared_size > self.server.max_asset_bytes:
                raise ValueError(f"byte_size must be between 1 and {self.server.max_asset_bytes}")
            upload_id = uuid.uuid4().hex
            directory = _upload_directory(self.server, upload_id)
            directory.mkdir(parents=True, exist_ok=False)
            metadata = dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {}
            metadata.setdefault("source_agent_id", request.agent_id)
            metadata.setdefault("source_workspace_id", request.workspace_id)
            if request.subject_id:
                metadata.setdefault("subject_id", request.subject_id)
            manifest = {
                "version": 1,
                "upload_id": upload_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "profile_id": request.profile_id,
                "agent_id": request.agent_id,
                "workspace_id": request.workspace_id,
                "subject_id": request.subject_id,
                "channel_id": request.channel_id,
                "visibility_scope": "channel" if request.channel_id else "workspace",
                "media_type": str(payload.get("media_type") or "application/octet-stream"),
                "original_name": str(payload.get("original_name") or ""),
                "byte_size": declared_size,
                "sha256": str(payload.get("sha256") or "").strip().casefold(),
                "metadata": metadata,
            }
            _atomic_json_file(directory / "manifest.json", manifest)
            return {"status": "ok", "upload_id": upload_id, "chunk_size": self.server.asset_chunk_bytes, "byte_size": declared_size}

        upload_complete = _UPLOAD_COMPLETE_PATH.fullmatch(path)
        if upload_complete:
            principal = self._principal(payload, permission="assets")
            request = _request_identity(payload, principal, require_subject=False)
            upload_id = unquote(upload_complete.group(1)).strip()
            directory, manifest = _load_upload(self.server, upload_id)
            _upload_identity_matches(manifest, request)
            completed_path = directory / "completed.json"
            if completed_path.is_file():
                completed = json.loads(completed_path.read_text(encoding="utf-8"))
                if not isinstance(completed, dict):
                    raise ValueError("completed upload receipt is invalid")
                return completed
            parts = sorted(directory.glob("*.part"))
            if not parts or [path.name for path in parts] != [f"{index:08d}.part" for index in range(len(parts))]:
                raise ValueError("upload parts must be contiguous starting at zero")
            total = sum(path.stat().st_size for path in parts)
            if total != int(manifest.get("byte_size") or 0):
                raise ValueError("uploaded byte count does not match manifest")
            digest = hashlib.sha256()
            for part in parts:
                with part.open("rb") as source:
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
            expected = str(manifest.get("sha256") or "")
            if expected and not hmac.compare_digest(expected, digest.hexdigest()):
                raise ValueError("completed asset SHA-256 does not match manifest")
            from .spatial import store_asset

            stream = _PartsReader(parts)
            try:
                item = store_asset(
                    self.server.root,
                    stream,
                    profile_id=request.profile_id,
                    media_type=str(manifest.get("media_type") or "application/octet-stream"),
                    original_name=str(manifest.get("original_name") or ""),
                    metadata=manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else None,
                    max_bytes=self.server.max_asset_bytes,
                    visibility_scope=str(manifest.get("visibility_scope") or "workspace"),
                    channel_id=str(manifest.get("channel_id") or ""),
                    workspace_id=request.workspace_id,
                    owner_agent_id=request.agent_id,
                    source_subject_id=request.subject_id,
                    source_agent_id=request.agent_id,
                )
            finally:
                stream.close()
            if expected and not hmac.compare_digest(expected, str(item.get("sha256") or "")):
                raise ValueError("completed asset SHA-256 does not match manifest")
            result = {"status": "ok", "asset": item, "upload_id": upload_id, "deduplicated_completion": False}
            _atomic_json_file(completed_path, result)
            for part in parts:
                part.unlink(missing_ok=True)
            return result

        if path in {"/v1/maps", "/v1/spatial-observations"}:
            permission = "maps" if path == "/v1/maps" else "spatial"
            principal = self._principal(payload, permission=permission)
            request = _request_identity(payload, principal, require_subject=False)
            channel_id = str(payload.get("channel_id") or "")
            _channel_in_scope(self.server, principal, request, channel_id)
            if path == "/v1/maps":
                from .spatial import create_map_version

                asset_id = str(payload.get("asset_id") or "")
                if asset_id:
                    from .spatial import get_asset

                    if not get_asset(
                        self.server.root,
                        profile_id=request.profile_id,
                        asset_id=asset_id,
                        enforce_visibility=True,
                        channel_id=channel_id,
                        workspace_id=request.workspace_id,
                        viewer_agent_id=request.agent_id,
                    ):
                        raise PermissionError("asset is outside this token scope")

                item = create_map_version(
                    self.server.root,
                    profile_id=request.profile_id,
                    channel_id=channel_id,
                    map_id=str(payload.get("map_id") or ""),
                    coordinate_frame=str(payload.get("coordinate_frame") or ""),
                    version=int(payload["version"]) if payload.get("version") is not None else None,
                    name=str(payload.get("name") or ""),
                    asset_id=asset_id,
                    source_workspace_id=request.workspace_id,
                    source_agent_id=request.agent_id,
                    captured_at=payload.get("captured_at"),
                    metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
                    idempotency_key=str(payload.get("idempotency_key") or ""),
                )
                return {"status": "ok", "map": item}
            from .spatial import record_spatial_observation

            raw_asset_ids = payload.get("asset_ids") if isinstance(payload.get("asset_ids"), list) else []
            asset_ids = [str(value).strip() for value in raw_asset_ids if str(value).strip()]
            explicit_asset = str(payload.get("asset_id") or "").strip()
            if explicit_asset and explicit_asset not in asset_ids:
                asset_ids.insert(0, explicit_asset)
            asset_id = asset_ids[0] if asset_ids else ""
            if asset_ids:
                from .spatial import get_asset

                for candidate_asset_id in asset_ids:
                    if not get_asset(
                        self.server.root,
                        profile_id=request.profile_id,
                        asset_id=candidate_asset_id,
                        enforce_visibility=True,
                        channel_id=channel_id,
                        workspace_id=request.workspace_id,
                        viewer_agent_id=request.agent_id,
                    ):
                        raise PermissionError("asset is outside this token scope")
            metadata = dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {}
            if len(asset_ids) > 1:
                metadata["related_asset_ids"] = [str(value) for value in asset_ids[1:] if str(value)]
            item = record_spatial_observation(
                self.server.root,
                profile_id=request.profile_id,
                channel_id=channel_id,
                workspace_id=request.workspace_id,
                subject_id=request.subject_id,
                source_agent_id=request.agent_id,
                source_ref=str(payload.get("source_ref") or ""),
                observation_kind=str(payload.get("observation_kind") or "spatial_observation"),
                owner_agent_id=str(payload.get("owner_agent_id") or ""),
                map_version_id=str(payload.get("map_version_id") or ""),
                map_id=str(payload.get("map_id") or ""),
                map_version=int(payload["map_version"]) if payload.get("map_version") is not None else None,
                asset_id=asset_id,
                location_id=str(payload.get("location_id") or ""),
                location_text=str(payload.get("location_text") or ""),
                caption=str(payload.get("caption") or payload.get("content") or ""),
                ocr_text=str(payload.get("ocr_text") or ""),
                objects=payload.get("objects") if isinstance(payload.get("objects"), list) else None,
                confidence=payload.get("confidence"),
                observed_at=payload.get("observed_at"),
                valid_until=payload.get("valid_until"),
                visibility_scope=str(payload.get("visibility_scope") or "channel"),
                supersedes_observation_id=str(payload.get("supersedes_observation_id") or ""),
                idempotency_key=str(payload.get("idempotency_key") or ""),
                metadata=metadata,
            )
            return {"status": "ok", "observation": item}

        permission = (
            "read" if path == "/v1/retrieve" else
            "record" if path == "/v1/events" else
            "remember" if path == "/v1/remember" else
            "feedback" if path == "/v1/feedback" else
            "proposals"
        )
        principal = self._principal(payload, permission=permission)
        require_subject = path in {"/v1/retrieve", "/v1/events", "/v1/remember"}
        request = _request_identity(payload, principal, require_subject=require_subject)

        if path == "/v1/retrieve":
            return retrieve(retrieval_args(self.server.root, payload, request.profile_id, request.workspace_id, request.agent_id))
        if path == "/v1/events":
            content = str(payload.get("content") or "").strip()
            if not content:
                raise ValueError("content is required")
            return insert_raw_event(
                self.server.root,
                subject_id=request.subject_id,
                subject_name=str(payload.get("subject_name") or "Unknown"),
                session_id=str(payload.get("session_id") or ""),
                source_type=str(payload.get("source_type") or "api"),
                source_ref=str(payload.get("source_ref") or ""),
                topic_hint=str(payload.get("topic_hint") or ""),
                domain_hint=str(payload.get("domain_hint") or ""),
                event_time=str(payload.get("event_time") or ""),
                content=content,
                allow_duplicate=bool(payload.get("allow_duplicate", False)),
                profile_id=request.profile_id,
                workspace_id=request.workspace_id,
                origin_agent_id=request.agent_id,
                visibility_scope=str(payload.get("visibility_scope") or "workspace"),
                event_uid=str(payload.get("event_uid") or ""),
                idempotency_key=str(payload.get("idempotency_key") or ""),
                shared_mode=True,
            )
        if path == "/v1/remember":
            return remember_memory(remember_args(self.server.root, payload, request.profile_id, request.workspace_id, request.agent_id))
        if path == "/v1/feedback":
            claim_id = str(payload.get("claim_id") or "").strip()
            if not claim_id:
                raise ValueError("claim_id is required")
            _claim_in_scope(self.server, request, claim_id, allowed_subjects=principal.subject_ids)
            return record_feedback(
                self.server.root,
                claim_id=claim_id,
                feedback_type=str(payload.get("feedback_type") or ""),
                source=f"api:{request.agent_id}",
                note=str(payload.get("note") or ""),
                retrieval_uid=str(payload.get("retrieval_uid") or ""),
            )
        if path == "/v1/proposals/list":
            proposals = []
            for item in list_proposals(self.server.root):
                proposal = get_proposal(self.server.root, str(item.get("id") or ""))
                if proposal and _proposal_in_scope(self.server, request, proposal, allowed_subjects=principal.subject_ids):
                    proposals.append(item)
            return {"status": "ok", "proposals": proposals}

        approve_match = _PROPOSAL_APPROVE_PATH.fullmatch(path)
        if approve_match:
            proposal_id = unquote(approve_match.group(1)).strip()
            proposal = get_proposal(self.server.root, proposal_id)
            if not proposal:
                raise ResourceNotFound("Proposal not found.")
            if not _proposal_in_scope(self.server, request, proposal, allowed_subjects=principal.subject_ids):
                raise PermissionError("proposal is outside this token scope")
            return approve_memory_proposal(self.server.root, proposal_id)
        raise ResourceNotFound("Route not found.")


class APIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        root: Path,
        tokens: Mapping[str, Principal],
        *,
        config: AppConfig | None = None,
        max_asset_bytes: int = DEFAULT_MAX_ASSET_BYTES,
        asset_chunk_bytes: int = DEFAULT_ASSET_CHUNK_BYTES,
        tls_cert: str | Path | None = None,
        tls_key: str | Path | None = None,
        access_log: bool = False,
        log_stream: TextIO | None = None,
    ) -> None:
        resolved_root = Path(root).expanduser().resolve()
        base = config or load_config()
        self.root = resolved_root
        self.tokens = dict(tokens)
        self.config = replace(base, store=resolved_root)
        self.max_asset_bytes = max(1, int(max_asset_bytes))
        self.asset_chunk_bytes = max(
            1,
            min(int(asset_chunk_bytes), 8 * 1024 * 1024, self.max_asset_bytes),
        )
        self.access_log = bool(access_log)
        self.log_stream = log_stream or sys.stderr
        self._log_lock = threading.Lock()
        self._request_condition = threading.Condition()
        self._active_requests = 0
        self._serving = threading.Event()
        self._shutdown_requested = threading.Event()
        self._shutdown_reason = ""
        super().__init__(address, MemoryAPI)
        if tls_cert or tls_key:
            if not tls_cert or not tls_key:
                self.server_close()
                raise ValueError("tls_cert and tls_key must be supplied together")
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(Path(tls_cert).expanduser()), str(Path(tls_key).expanduser()))
            self.socket = context.wrap_socket(self.socket, server_side=True)

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        if self._shutdown_requested.is_set():
            return
        self._serving.set()
        try:
            super().serve_forever(poll_interval=poll_interval)
        finally:
            self._serving.clear()

    def process_request(self, request: Any, client_address: Any) -> None:
        # Count the accepted socket before its worker starts.  Counting only
        # inside the handler leaves a scheduling race where shutdown can see
        # zero active work immediately before a new worker begins.
        self.request_started()
        try:
            super().process_request(request, client_address)
        except Exception:
            self.request_finished()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_finished()

    def request_started(self) -> None:
        with self._request_condition:
            self._active_requests += 1

    def request_finished(self) -> None:
        with self._request_condition:
            self._active_requests = max(0, self._active_requests - 1)
            if not self._active_requests:
                self._request_condition.notify_all()

    def wait_for_requests(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._request_condition:
            while self._active_requests:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._request_condition.wait(remaining)
            return True

    def log_event(self, event: str, **fields: Any) -> None:
        if not self.access_log:
            return
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": str(event),
            **fields,
        }
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._log_lock:
            print(encoded, file=self.log_stream, flush=True)

    def handle_error(self, _request: Any, _client_address: Any) -> None:
        # socketserver's default prints a free-form traceback.  Keep hosted
        # service output machine-readable and avoid serializing request state.
        error_type = getattr(sys.exc_info()[0], "__name__", "Exception")
        self.log_event("http_connection_error", error_type=error_type)

    def log_request_event(
        self,
        *,
        request_id: str,
        method: str,
        path: str,
        status: int,
        duration_ms: float,
        error_type: str = "",
    ) -> None:
        safe_path = path
        for token in self.tokens:
            if token:
                safe_path = safe_path.replace(token, "[REDACTED]")
        safe_request_id = request_id
        if any(
            _secret_equal(request_id, token)
            for token in self.tokens
            if request_id
        ):
            safe_request_id = "[REDACTED]"
        fields: dict[str, Any] = {
            "request_id": safe_request_id,
            "method": method,
            # Query strings are intentionally excluded: identities and future
            # credentials must never leak through an operator access log.
            "path": safe_path,
            "status": int(status),
            "duration_ms": duration_ms,
        }
        if error_type:
            fields["error_type"] = error_type
        self.log_event("http_request", **fields)

    def request_shutdown(self, *, reason: str) -> bool:
        """Ask serve_forever to stop without calling shutdown on its thread."""

        if self._shutdown_requested.is_set():
            return False
        self._shutdown_reason = str(reason or "requested")
        self._shutdown_requested.set()
        self.log_event("server_shutdown_requested", reason=self._shutdown_reason)
        if self._serving.is_set():
            threading.Thread(
                target=self.shutdown,
                name="meta-memory-http-shutdown",
                daemon=True,
            ).start()
        return True


@contextmanager
def _graceful_signal_handlers(server: APIServer) -> Iterator[None]:
    """Install process signal handlers only where Python permits doing so."""

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous: dict[signal.Signals, Any] = {}

    def stop(signum: int, _frame: object) -> None:
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        server.request_shutdown(reason=f"signal:{name}")

    try:
        for candidate in (signal.SIGTERM, signal.SIGINT):
            previous[candidate] = signal.getsignal(candidate)
            signal.signal(candidate, stop)
        yield
    finally:
        for candidate, handler in previous.items():
            signal.signal(candidate, handler)


def create_server(
    address: tuple[str, int],
    *,
    store: str | Path,
    agents_file: str | Path,
    config: AppConfig | None = None,
    max_asset_bytes: int = DEFAULT_MAX_ASSET_BYTES,
    asset_chunk_bytes: int = DEFAULT_ASSET_CHUNK_BYTES,
    tls_cert: str | Path | None = None,
    tls_key: str | Path | None = None,
    access_log: bool = False,
    log_stream: TextIO | None = None,
) -> APIServer:
    """Create a configured server without starting its blocking loop."""

    return APIServer(
        address,
        Path(store).expanduser().resolve(),
        load_principals(Path(agents_file).expanduser().resolve()),
        config=config,
        max_asset_bytes=max_asset_bytes,
        asset_chunk_bytes=asset_chunk_bytes,
        tls_cert=tls_cert,
        tls_key=tls_key,
        access_log=access_log,
        log_stream=log_stream,
    )


def serve(
    *,
    host: str,
    port: int,
    store: str | Path,
    agents_file: str | Path,
    config: AppConfig | None = None,
    max_asset_bytes: int = DEFAULT_MAX_ASSET_BYTES,
    asset_chunk_bytes: int = DEFAULT_ASSET_CHUNK_BYTES,
    tls_cert: str | Path | None = None,
    tls_key: str | Path | None = None,
    access_log: bool | None = None,
    shutdown_timeout: float | None = None,
) -> None:
    """Run the central HTTP transport until interrupted."""

    server = create_server(
        (host, int(port)),
        store=store,
        agents_file=agents_file,
        config=config,
        max_asset_bytes=max_asset_bytes,
        asset_chunk_bytes=asset_chunk_bytes,
        tls_cert=tls_cert,
        tls_key=tls_key,
        access_log=(
            _environment_enabled(ACCESS_LOG_ENV, default=True)
            if access_log is None
            else bool(access_log)
        ),
    )
    print(
        json.dumps(
            {
                "status": "listening",
                "scheme": "https" if tls_cert else "http",
                "host": host,
                "port": server.server_address[1],
                "store": str(server.root),
                "max_asset_bytes": server.max_asset_bytes,
                "asset_chunk_bytes": server.asset_chunk_bytes,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    graceful_seconds = _shutdown_timeout() if shutdown_timeout is None else max(
        0.0, min(float(shutdown_timeout), 300.0)
    )
    drained = True
    try:
        with _graceful_signal_handlers(server):
            if not server._shutdown_requested.is_set():
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    server.request_shutdown(reason="keyboard_interrupt")
    finally:
        drained = server.wait_for_requests(graceful_seconds)
        server.log_event(
            "server_stopped",
            reason=server._shutdown_reason or "serve_forever_returned",
            requests_drained=drained,
            graceful_timeout_seconds=graceful_seconds,
        )
        server.server_close()


def main(argv: Iterable[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the authenticated Meta Memory HTTP API.")
    parser.add_argument("--store", help="Central memory-data directory; defaults to the configured store")
    parser.add_argument("--config", help="Optional Meta Memory config.toml used for runtime policy")
    parser.add_argument("--agents-file", required=True, help="Private JSON file based on extras/http/agents.example.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--max-asset-mb", type=int, default=64, help="Maximum one asset may occupy")
    parser.add_argument("--asset-chunk-mb", type=int, default=2, help="Maximum resumable upload part size")
    parser.add_argument("--tls-cert", help="PEM certificate for native HTTPS; usually a reverse proxy terminates TLS")
    parser.add_argument("--tls-key", help="PEM private key used with --tls-cert")
    args = parser.parse_args(list(argv) if argv is not None else None)
    config = load_config(args.config)
    root = Path(args.store).expanduser().resolve() if args.store else config.store
    serve(
        host=args.host,
        port=args.port,
        store=root,
        agents_file=args.agents_file,
        config=config,
        max_asset_bytes=max(1, int(args.max_asset_mb)) * 1024 * 1024,
        asset_chunk_bytes=max(1, int(args.asset_chunk_mb)) * 1024 * 1024,
        tls_cert=args.tls_cert,
        tls_key=args.tls_key,
    )


__all__ = [
    "APIServer", "MemoryAPI", "Principal", "RequestIdentity", "authorize", "create_server",
    "identity", "load_principals", "main", "remember_args", "retrieval_args", "serve",
]


if __name__ == "__main__":
    main()
