"""One explicit runtime identity for every read and write boundary.

The store is shared by agents, but identity is never inferred from content or
from a default session.  This module deliberately contains the visibility
predicate used by both retrieval and projections so the rules do not drift.
"""
from __future__ import annotations

from dataclasses import dataclass


VISIBILITY_SCOPES = {"global", "workspace", "agent"}


@dataclass(frozen=True)
class RuntimeIdentity:
    profile_id: str = "default"
    workspace_id: str = "default"
    agent_id: str = ""

    def __post_init__(self) -> None:
        if not self.profile_id.strip() or not self.workspace_id.strip():
            raise ValueError("profile_id and workspace_id are required.")


def identity_from(*, profile_id: str = "default", workspace_id: str = "default", agent_id: str = "") -> RuntimeIdentity:
    return RuntimeIdentity(str(profile_id or "default"), str(workspace_id or "default"), str(agent_id or ""))


def validate_visibility(visibility_scope: str, owner_agent_id: str = "") -> str:
    value = str(visibility_scope or "workspace").casefold()
    if value not in VISIBILITY_SCOPES:
        raise ValueError(f"Unsupported visibility_scope: {visibility_scope!r}")
    if value == "agent" and not str(owner_agent_id).strip():
        raise ValueError("agent-visible memory requires owner_agent_id.")
    return value


def visibility_sql(identity: RuntimeIdentity, *, alias: str = "", owner_column: str = "owner_agent_id") -> tuple[str, list[object]]:
    """Return a conservative visibility predicate and its parameters.

    Global records are profile-wide; workspace records are limited to the
    matching workspace; agent records are limited to their explicit owner.
    """
    prefix = f"{alias}." if alias else ""
    return (
        f"{prefix}profile_id=? AND ("
        f"{prefix}visibility_scope='global' OR "
        f"({prefix}visibility_scope='workspace' AND {prefix}workspace_id=?) OR "
        f"({prefix}visibility_scope='agent' AND {prefix}{owner_column}=?))",
        [identity.profile_id, identity.workspace_id, identity.agent_id],
    )


def add_identity_args(parser, *, include_visibility: bool = False, include_shared_mode: bool = False) -> None:
    parser.add_argument("--profile-id", default="default", help="Profile identity scope")
    parser.add_argument("--workspace-id", default="default", help="Workspace identity scope")
    parser.add_argument("--agent-id", default="", help="Authenticated/originating agent identity")
    if include_visibility:
        parser.add_argument("--visibility-scope", choices=sorted(VISIBILITY_SCOPES), default="workspace")
    if include_shared_mode:
        parser.add_argument("--shared-mode", action="store_true", help="Require a non-empty, host-generated session id")
