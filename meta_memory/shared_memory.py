"""Explicit shared-world channels, curated activity, and temporal state.

This data plane is intentionally adjacent to the existing Claim pipeline.  It
does not widen Claim visibility or copy raw Agent conversations.  Applications
publish only the compact activity/state records they want other Agents to use.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .legacy import bootstrap

bootstrap()
from _common import open_db  # type: ignore  # noqa: E402


AUDIENCE_TYPES = frozenset({"user", "household", "person", "project", "device", "agent", "session", "event"})
CHANNEL_TYPES = AUDIENCE_TYPES
MEMBER_TYPES = frozenset({"profile", "agent", "subject"})


def _identifier(value: object, name: str, *, maximum: int = 256) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    if len(text) > maximum or any(ord(char) < 32 for char in text):
        raise ValueError(f"{name} is invalid")
    return text


def _enum(value: object, name: str, allowed: frozenset[str]) -> str:
    text = _identifier(value, name).casefold()
    if text not in allowed:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(allowed))}")
    return text


def _json(value: Any, *, object_only: bool = False) -> str:
    if value is None:
        value = {}
    if object_only and not isinstance(value, Mapping):
        raise ValueError("metadata must be a JSON object")
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("value is not JSON serializable") from exc


def _timestamp(value: str | datetime | None = None) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        parsed = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"invalid ISO timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _optional_timestamp(value: str | datetime | None) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _timestamp(value)


def _confidence(value: float | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    return number


def _importance(value: float) -> float:
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise ValueError("importance must be between 0 and 1")
    return number


def _connection(store: str | Path) -> sqlite3.Connection:
    conn = open_db(Path(store).expanduser().resolve())
    conn.row_factory = sqlite3.Row
    return conn


def _decoded(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in tuple(result):
        if key.endswith("_json"):
            output_key = key[:-5]
            try:
                result[output_key] = json.loads(str(result.pop(key) or "{}"))
            except json.JSONDecodeError:
                result[output_key] = {}
    return result


def _channel(conn: sqlite3.Connection, *, profile_id: str, channel_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM memory_channels WHERE profile_id=? AND channel_id=? AND status='active'",
        (profile_id, channel_id),
    ).fetchone()
    if not row:
        raise KeyError(f"active channel not found: {channel_id}")
    return row


def ensure_audience(
    store: str | Path,
    *,
    profile_id: str,
    audience_type: str,
    audience_key: str,
    label: str = "",
    metadata: Mapping[str, Any] | None = None,
    profile_wide: bool = True,
) -> dict[str, Any]:
    """Create or reactivate a stable audience.

    ``profile_wide=True`` is the convenient shared-memory default.  Revoke the
    generated profile membership and grant selected Agent/subject memberships
    when a narrower household or person audience is required.
    """

    profile = _identifier(profile_id, "profile_id")
    kind = _enum(audience_type, "audience_type", AUDIENCE_TYPES)
    key = _identifier(audience_key, "audience_key")
    conn = _connection(store)
    created = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM memory_audiences WHERE profile_id=? AND audience_type=? AND audience_key=?",
            (profile, kind, key),
        ).fetchone()
        if row:
            audience_id = str(row["audience_id"])
            updates = ["status='active'", "updated_at=?"]
            values: list[Any] = [_timestamp()]
            if label:
                updates.append("label=?")
                values.append(str(label).strip())
            if metadata is not None:
                updates.append("metadata_json=?")
                values.append(_json(metadata, object_only=True))
            values.append(audience_id)
            conn.execute(f"UPDATE memory_audiences SET {', '.join(updates)} WHERE audience_id=?", values)
        else:
            created = True
            audience_id = uuid.uuid4().hex
            now = _timestamp()
            conn.execute(
                """INSERT INTO memory_audiences(
                       audience_id,profile_id,audience_type,audience_key,label,metadata_json,status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,'active',?,?)""",
                (audience_id, profile, kind, key, str(label).strip(), _json(metadata, object_only=True), now, now),
            )
        if profile_wide:
            conn.execute(
                "INSERT OR IGNORE INTO memory_audience_members(audience_id,member_type,member_id,created_at) VALUES(?,'profile',?,?)",
                (audience_id, profile, _timestamp()),
            )
        row = conn.execute("SELECT * FROM memory_audiences WHERE audience_id=?", (audience_id,)).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    result = _decoded(row) or {}
    result["created"] = created
    return result


def grant_audience_member(
    store: str | Path,
    *,
    profile_id: str,
    audience_id: str,
    member_type: str,
    member_id: str,
) -> dict[str, Any]:
    profile = _identifier(profile_id, "profile_id")
    audience = _identifier(audience_id, "audience_id")
    kind = _enum(member_type, "member_type", MEMBER_TYPES)
    member = _identifier(member_id, "member_id")
    conn = _connection(store)
    try:
        row = conn.execute("SELECT 1 FROM memory_audiences WHERE profile_id=? AND audience_id=?", (profile, audience)).fetchone()
        if not row:
            raise KeyError(f"audience not found: {audience}")
        cursor = conn.execute(
            "INSERT OR IGNORE INTO memory_audience_members(audience_id,member_type,member_id,created_at) VALUES(?,?,?,?)",
            (audience, kind, member, _timestamp()),
        )
        conn.commit()
        return {"audience_id": audience, "member_type": kind, "member_id": member, "created": cursor.rowcount > 0}
    finally:
        conn.close()


def revoke_audience_member(
    store: str | Path,
    *,
    profile_id: str,
    audience_id: str,
    member_type: str,
    member_id: str,
) -> bool:
    profile = _identifier(profile_id, "profile_id")
    audience = _identifier(audience_id, "audience_id")
    kind = _enum(member_type, "member_type", MEMBER_TYPES)
    member = _identifier(member_id, "member_id")
    conn = _connection(store)
    try:
        cursor = conn.execute(
            """DELETE FROM memory_audience_members
               WHERE audience_id=? AND member_type=? AND member_id=?
                 AND EXISTS(SELECT 1 FROM memory_audiences a WHERE a.audience_id=? AND a.profile_id=?)""",
            (audience, kind, member, audience, profile),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def audience_ids_for_member(
    store: str | Path,
    *,
    profile_id: str,
    member_type: str,
    member_id: str,
) -> list[str]:
    profile = _identifier(profile_id, "profile_id")
    kind = _enum(member_type, "member_type", MEMBER_TYPES)
    member = _identifier(member_id, "member_id")
    conn = _connection(store)
    try:
        rows = conn.execute(
            """SELECT DISTINCT a.audience_id
               FROM memory_audiences a JOIN memory_audience_members m ON m.audience_id=a.audience_id
               WHERE a.profile_id=? AND a.status='active'
                 AND ((m.member_type=? AND m.member_id=?) OR (m.member_type='profile' AND m.member_id=?))
               ORDER BY a.audience_id""",
            (profile, kind, member, profile),
        ).fetchall()
        return [str(row[0]) for row in rows]
    finally:
        conn.close()


def ensure_channel(
    store: str | Path,
    *,
    profile_id: str,
    channel_type: str,
    channel_key: str,
    audience_id: str = "",
    subject_id: str = "",
    workspace_id: str = "",
    owner_agent_id: str = "",
    label: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    profile = _identifier(profile_id, "profile_id")
    kind = _enum(channel_type, "channel_type", CHANNEL_TYPES)
    key = _identifier(channel_key, "channel_key")
    if not audience_id:
        audience_id = str(
            ensure_audience(
                store,
                profile_id=profile,
                audience_type=kind,
                audience_key=key,
                label=label,
            )["audience_id"]
        )
    audience = _identifier(audience_id, "audience_id")
    conn = _connection(store)
    created = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute(
            "SELECT 1 FROM memory_audiences WHERE audience_id=? AND profile_id=? AND status='active'",
            (audience, profile),
        ).fetchone():
            raise KeyError(f"active audience not found: {audience}")
        row = conn.execute(
            "SELECT * FROM memory_channels WHERE profile_id=? AND channel_type=? AND channel_key=?",
            (profile, kind, key),
        ).fetchone()
        now = _timestamp()
        if row:
            channel_id = str(row["channel_id"])
            if str(row["audience_id"] or "") != audience:
                raise ValueError(
                    "an existing channel cannot be rebound to a different audience; "
                    "create a new channel key instead"
                )
            conn.execute(
                """UPDATE memory_channels SET subject_id=?,workspace_id=?,owner_agent_id=?,
                       label=CASE WHEN ?!='' THEN ? ELSE label END,
                       metadata_json=CASE WHEN ? IS NOT NULL THEN ? ELSE metadata_json END,
                       status='active',updated_at=? WHERE channel_id=?""",
                (
                    str(subject_id).strip(), str(workspace_id).strip(), str(owner_agent_id).strip(),
                    str(label).strip(), str(label).strip(), 1 if metadata is not None else None,
                    _json(metadata, object_only=True), now, channel_id,
                ),
            )
        else:
            created = True
            channel_id = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO memory_channels(
                       channel_id,profile_id,channel_type,channel_key,audience_id,subject_id,workspace_id,
                       owner_agent_id,label,metadata_json,status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,'active',?,?)""",
                (
                    channel_id, profile, kind, key, audience, str(subject_id).strip(), str(workspace_id).strip(),
                    str(owner_agent_id).strip(), str(label).strip(), _json(metadata, object_only=True), now, now,
                ),
            )
        row = conn.execute("SELECT * FROM memory_channels WHERE channel_id=?", (channel_id,)).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    result = _decoded(row) or {}
    result["created"] = created
    return result


def list_channels(
    store: str | Path,
    *,
    profile_id: str,
    audience_id: str = "",
    channel_type: str = "",
    member_type: str = "",
    member_id: str = "",
) -> list[dict[str, Any]]:
    profile = _identifier(profile_id, "profile_id")
    clauses = ["c.profile_id=?", "c.status='active'", "a.status='active'"]
    values: list[Any] = [profile]
    if audience_id:
        clauses.append("c.audience_id=?")
        values.append(_identifier(audience_id, "audience_id"))
    if channel_type:
        clauses.append("c.channel_type=?")
        values.append(_enum(channel_type, "channel_type", CHANNEL_TYPES))
    if member_type or member_id:
        kind = _enum(member_type, "member_type", MEMBER_TYPES)
        member = _identifier(member_id, "member_id")
        clauses.append(
            "EXISTS(SELECT 1 FROM memory_audience_members m WHERE m.audience_id=c.audience_id "
            "AND ((m.member_type=? AND m.member_id=?) OR (m.member_type='profile' AND m.member_id=?)))"
        )
        values.extend([kind, member, profile])
    conn = _connection(store)
    try:
        rows = conn.execute(
            "SELECT c.* FROM memory_channels c JOIN memory_audiences a ON a.audience_id=c.audience_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY c.channel_type,c.channel_key",
            values,
        ).fetchall()
        return [_decoded(row) or {} for row in rows]
    finally:
        conn.close()


def publish_activity(
    store: str | Path,
    *,
    profile_id: str,
    channel_id: str,
    summary: str,
    source_workspace_id: str = "",
    subject_id: str = "",
    source_agent_id: str = "",
    source_session_id: str = "",
    source_ref: str = "",
    confidence: float | None = None,
    activity_kind: str = "update",
    title: str = "",
    payload: Mapping[str, Any] | None = None,
    importance: float = 0.5,
    occurred_at: str | datetime | None = None,
    valid_until: str | datetime | None = None,
    supersedes_activity_id: str = "",
    idempotency_key: str = "",
) -> dict[str, Any]:
    profile = _identifier(profile_id, "profile_id")
    channel = _identifier(channel_id, "channel_id")
    text = str(summary or "").strip()
    if not text:
        raise ValueError("summary is required")
    source_agent = str(source_agent_id or "").strip()
    idem = str(idempotency_key or "").strip()
    if idem and not source_agent:
        raise ValueError("source_agent_id is required with idempotency_key")
    occurred = _timestamp(occurred_at)
    until = _optional_timestamp(valid_until)
    if until and until <= occurred:
        raise ValueError("valid_until must be later than occurred_at")
    status = "expired" if until and until <= _timestamp() else "active"
    conn = _connection(store)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _channel(conn, profile_id=profile, channel_id=channel)
        if idem:
            existing = conn.execute(
                "SELECT * FROM shared_activities WHERE profile_id=? AND source_agent_id=? AND idempotency_key=?",
                (profile, source_agent, idem),
            ).fetchone()
            if existing:
                conn.commit()
                result = _decoded(existing) or {}
                result["deduplicated"] = True
                return result
        supersedes = str(supersedes_activity_id or "").strip() or None
        if supersedes:
            previous = conn.execute(
                "SELECT activity_id FROM shared_activities WHERE activity_id=? AND profile_id=? AND channel_id=?",
                (supersedes, profile, channel),
            ).fetchone()
            if not previous:
                raise KeyError(f"activity to supersede not found in channel: {supersedes}")
        activity_id = uuid.uuid4().hex
        now = _timestamp()
        conn.execute(
            """INSERT INTO shared_activities(
                   activity_id,profile_id,channel_id,source_workspace_id,subject_id,source_agent_id,source_session_id,
                   activity_kind,title,summary,payload_json,importance,occurred_at,valid_until,status,
                   supersedes_activity_id,idempotency_key,created_at,updated_at,source_ref,confidence
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                activity_id, profile, channel, str(source_workspace_id).strip(), str(subject_id).strip(), source_agent,
                str(source_session_id).strip(), _identifier(activity_kind, "activity_kind", maximum=80),
                str(title).strip(), text, _json(payload, object_only=True), _importance(importance), occurred, until,
                status, supersedes, idem or None, now, now,
                str(source_ref or "").strip(), _confidence(confidence),
            ),
        )
        if supersedes and status == "active":
            conn.execute(
                "UPDATE shared_activities SET status='superseded',superseded_by_activity_id=?,updated_at=? WHERE activity_id=?",
                (activity_id, now, supersedes),
            )
        row = conn.execute("SELECT * FROM shared_activities WHERE activity_id=?", (activity_id,)).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    result = _decoded(row) or {}
    result["deduplicated"] = False
    return result


def list_activity_feed(
    store: str | Path,
    *,
    profile_id: str,
    channel_id: str = "",
    audience_id: str = "",
    member_type: str = "",
    member_id: str = "",
    source_workspace_id: str = "",
    subject_id: str = "",
    subject_ids: Iterable[str] | None = None,
    since: str | datetime | None = None,
    include_history: bool = False,
    now: str | datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    profile = _identifier(profile_id, "profile_id")
    clauses = ["x.profile_id=?"]
    values: list[Any] = [profile]
    moment = _timestamp(now)
    if not include_history:
        clauses.extend(["x.status='active'", "(x.valid_until IS NULL OR x.valid_until>?)"])
        values.append(moment)
    if channel_id:
        clauses.append("x.channel_id=?")
        values.append(_identifier(channel_id, "channel_id"))
    if audience_id:
        clauses.append("c.audience_id=?")
        values.append(_identifier(audience_id, "audience_id"))
    if source_workspace_id:
        clauses.append("x.source_workspace_id=?")
        values.append(str(source_workspace_id).strip())
    if subject_id:
        clauses.append("(x.subject_id='' OR x.subject_id=?)")
        values.append(str(subject_id).strip())
    elif subject_ids:
        allowed_subjects = sorted({str(value).strip() for value in subject_ids if str(value).strip()})
        if allowed_subjects:
            clauses.append(
                "(x.subject_id='' OR x.subject_id IN ("
                + ",".join("?" for _ in allowed_subjects)
                + "))"
            )
            values.extend(allowed_subjects)
    if since:
        clauses.append("x.occurred_at>=?")
        values.append(_timestamp(since))
    if member_type or member_id:
        kind = _enum(member_type, "member_type", MEMBER_TYPES)
        member = _identifier(member_id, "member_id")
        clauses.append(
            "EXISTS(SELECT 1 FROM memory_audience_members m WHERE m.audience_id=c.audience_id "
            "AND ((m.member_type=? AND m.member_id=?) OR (m.member_type='profile' AND m.member_id=?)))"
        )
        values.extend([kind, member, profile])
    count = max(1, min(int(limit), 1000))
    conn = _connection(store)
    try:
        rows = conn.execute(
            "SELECT x.* FROM shared_activities x JOIN memory_channels c ON c.channel_id=x.channel_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY x.occurred_at DESC,x.created_at DESC LIMIT ?",
            [*values, count],
        ).fetchall()
        return [_decoded(row) or {} for row in rows]
    finally:
        conn.close()


def publish_temporal_state(
    store: str | Path,
    *,
    profile_id: str,
    channel_id: str,
    subject_id: str,
    state_key: str,
    value: Any,
    summary: str = "",
    source_workspace_id: str = "",
    source_agent_id: str = "",
    source_ref: str = "",
    confidence: float | None = None,
    observed_at: str | datetime | None = None,
    valid_from: str | datetime | None = None,
    valid_until: str | datetime | None = None,
    supersedes_state_id: str = "",
    idempotency_key: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    profile = _identifier(profile_id, "profile_id")
    channel = _identifier(channel_id, "channel_id")
    subject = _identifier(subject_id, "subject_id")
    key = _identifier(state_key, "state_key")
    source_agent = str(source_agent_id or "").strip()
    idem = str(idempotency_key or "").strip()
    if idem and not source_agent:
        raise ValueError("source_agent_id is required with idempotency_key")
    observed = _timestamp(observed_at)
    explicit_start = bool(valid_from and str(valid_from).strip())
    starts = _timestamp(valid_from if explicit_start else observed)
    until = _optional_timestamp(valid_until)
    if until and until <= starts:
        raise ValueError("valid_until must be later than valid_from")
    now = _timestamp()
    conn = _connection(store)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _channel(conn, profile_id=profile, channel_id=channel)
        if idem:
            existing = conn.execute(
                "SELECT * FROM temporal_states WHERE profile_id=? AND source_agent_id=? AND idempotency_key=?",
                (profile, source_agent, idem),
            ).fetchone()
            if existing:
                conn.commit()
                result = _decoded(existing) or {}
                result.update({"deduplicated": True, "is_current": str(existing["status"]) == "active"})
                return result
        current = conn.execute(
            """SELECT * FROM temporal_states
               WHERE profile_id=? AND channel_id=? AND subject_id=? AND state_key=? AND status='active'""",
            (profile, channel, subject, key),
        ).fetchone()
        latest = conn.execute(
            """SELECT * FROM temporal_states
               WHERE profile_id=? AND channel_id=? AND subject_id=? AND state_key=?
                 AND status!='staging' AND (status!='scheduled' OR valid_from<=?)
               ORDER BY observed_at DESC,created_at DESC LIMIT 1""",
            (profile, channel, subject, key, now),
        ).fetchone()
        explicit = str(supersedes_state_id or "").strip()
        if explicit:
            previous = conn.execute(
                """SELECT * FROM temporal_states WHERE state_id=? AND profile_id=? AND channel_id=?
                   AND subject_id=? AND state_key=? AND status='active'""",
                (explicit, profile, channel, subject, key),
            ).fetchone()
            if not previous or (current and str(current["state_id"]) != explicit):
                raise KeyError(f"active state to supersede not found for this key: {explicit}")
        else:
            previous = current
        state_id = uuid.uuid4().hex
        status = "active"
        supersedes: str | None = None
        superseded_by: str | None = None
        if until and until <= now:
            status = "expired"
        elif explicit_start and starts > now:
            # A scheduled fact is history until its declared validity begins;
            # it must not retire the current fact early.
            status = "scheduled"
        elif latest and str(latest["observed_at"]) >= observed and not explicit:
            # Late/out-of-order delivery never resurrects an older fact, even
            # after the newer state has expired and is no longer active.
            status = "superseded"
            superseded_by = str(latest["state_id"])
        elif previous:
            supersedes = str(previous["state_id"])
        insert_status = "staging" if supersedes and status == "active" else status
        conn.execute(
            """INSERT INTO temporal_states(
                   state_id,profile_id,channel_id,subject_id,state_key,value_json,summary,source_workspace_id,
                   source_agent_id,source_ref,confidence,observed_at,valid_from,valid_until,status,
                   supersedes_state_id,superseded_by_state_id,idempotency_key,metadata_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                state_id, profile, channel, subject, key, _json(value), str(summary).strip(),
                str(source_workspace_id).strip(), source_agent, str(source_ref).strip(), _confidence(confidence),
                observed, starts, until, insert_status, supersedes, superseded_by, idem or None,
                _json(metadata, object_only=True), now, now,
            ),
        )
        if supersedes and status == "active":
            # The new row must exist before the old row can reference it, while
            # the old active row must be retired before the partial unique
            # current-state index allows the new one to become active.
            conn.execute(
                "UPDATE temporal_states SET status='superseded',superseded_by_state_id=?,updated_at=? WHERE state_id=?",
                (state_id, now, supersedes),
            )
            conn.execute("UPDATE temporal_states SET status='active' WHERE state_id=?", (state_id,))
        row = conn.execute("SELECT * FROM temporal_states WHERE state_id=?", (state_id,)).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    result = _decoded(row) or {}
    result.update({"deduplicated": False, "is_current": str(result.get("status")) == "active"})
    return result


def list_temporal_states(
    store: str | Path,
    *,
    profile_id: str,
    channel_id: str = "",
    subject_id: str = "",
    subject_ids: Iterable[str] | None = None,
    state_key: str = "",
    current_only: bool = True,
    now: str | datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    profile = _identifier(profile_id, "profile_id")
    clauses = ["s.profile_id=?"]
    values: list[Any] = [profile]
    if current_only:
        moment = _timestamp(now)
        clauses.extend(
            [
                "(s.status='active' OR (s.status='scheduled' AND s.valid_from<=?))",
                "(s.valid_until IS NULL OR s.valid_until>?)",
                "NOT EXISTS(SELECT 1 FROM temporal_states newer "
                "WHERE newer.profile_id=s.profile_id AND newer.channel_id=s.channel_id "
                "AND newer.subject_id=s.subject_id AND newer.state_key=s.state_key "
                "AND (newer.status='active' OR (newer.status='scheduled' AND newer.valid_from<=?)) "
                "AND (newer.valid_until IS NULL OR newer.valid_until>?) "
                "AND (newer.valid_from>s.valid_from OR "
                "(newer.valid_from=s.valid_from AND newer.observed_at>s.observed_at) OR "
                "(newer.valid_from=s.valid_from AND newer.observed_at=s.observed_at "
                "AND newer.created_at>s.created_at)))",
            ]
        )
        values.extend([moment, moment, moment, moment])
    if channel_id:
        clauses.append("s.channel_id=?")
        values.append(_identifier(channel_id, "channel_id"))
    if subject_id:
        clauses.append("s.subject_id=?")
        values.append(str(subject_id).strip())
    elif subject_ids:
        allowed_subjects = sorted({str(value).strip() for value in subject_ids if str(value).strip()})
        if allowed_subjects:
            clauses.append("s.subject_id IN (" + ",".join("?" for _ in allowed_subjects) + ")")
            values.extend(allowed_subjects)
    if state_key:
        clauses.append("s.state_key=?")
        values.append(str(state_key).strip())
    count = max(1, min(int(limit), 1000))
    conn = _connection(store)
    try:
        rows = conn.execute(
            "SELECT s.* FROM temporal_states s WHERE " + " AND ".join(clauses)
            + " ORDER BY s.valid_from DESC,s.observed_at DESC,s.created_at DESC LIMIT ?",
            [*values, count],
        ).fetchall()
        return [_decoded(row) or {} for row in rows]
    finally:
        conn.close()


def get_current_state(
    store: str | Path,
    *,
    profile_id: str,
    channel_id: str,
    subject_id: str,
    state_key: str,
    now: str | datetime | None = None,
) -> dict[str, Any] | None:
    rows = list_temporal_states(
        store,
        profile_id=profile_id,
        channel_id=channel_id,
        subject_id=subject_id,
        state_key=state_key,
        current_only=True,
        now=now,
        limit=1,
    )
    return rows[0] if rows else None


def expire_time_bounded(
    store: str | Path,
    *,
    profile_id: str,
    now: str | datetime | None = None,
) -> dict[str, int]:
    """Materialize expiry for feeds, current state, and spatial observations."""

    profile = _identifier(profile_id, "profile_id")
    moment = _timestamp(now)
    conn = _connection(store)
    try:
        conn.execute("BEGIN IMMEDIATE")
        counts: dict[str, int] = {}
        for table, key in (
            ("shared_activities", "activities"),
            ("temporal_states", "states"),
            ("spatial_observations", "spatial_observations"),
        ):
            active_status = "status IN ('active','scheduled')" if table == "temporal_states" else "status='active'"
            cursor = conn.execute(
                f"UPDATE {table} SET status='expired',updated_at=? "
                f"WHERE profile_id=? AND {active_status} AND valid_until IS NOT NULL AND valid_until<=?",
                (moment, profile, moment),
            )
            counts[key] = max(0, cursor.rowcount)
        activated = 0
        scheduled = conn.execute(
            """SELECT * FROM temporal_states
               WHERE profile_id=? AND status='scheduled' AND valid_from<=?
                 AND (valid_until IS NULL OR valid_until>?)
               ORDER BY valid_from,observed_at,created_at""",
            (profile, moment, moment),
        ).fetchall()
        for row in scheduled:
            current = conn.execute(
                """SELECT * FROM temporal_states
                   WHERE profile_id=? AND channel_id=? AND subject_id=? AND state_key=?
                     AND status='active'""",
                (profile, row["channel_id"], row["subject_id"], row["state_key"]),
            ).fetchone()
            scheduled_rank = (str(row["valid_from"]), str(row["observed_at"]), str(row["created_at"]))
            current_rank = (
                (str(current["valid_from"]), str(current["observed_at"]), str(current["created_at"]))
                if current else None
            )
            if current is not None and current_rank is not None and current_rank >= scheduled_rank:
                conn.execute(
                    "UPDATE temporal_states SET status='superseded',superseded_by_state_id=?,updated_at=? "
                    "WHERE state_id=?",
                    (str(current["state_id"]), moment, str(row["state_id"])),
                )
                continue
            if current is not None:
                conn.execute(
                    "UPDATE temporal_states SET status='superseded',superseded_by_state_id=?,updated_at=? "
                    "WHERE state_id=?",
                    (str(row["state_id"]), moment, str(current["state_id"])),
                )
            conn.execute(
                "UPDATE temporal_states SET status='active',supersedes_state_id=?,updated_at=? WHERE state_id=?",
                (str(current["state_id"]) if current is not None else None, moment, str(row["state_id"])),
            )
            activated += 1
        counts["states_activated"] = activated
        conn.commit()
        return counts
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _bounded(value: Any, maximum: int = 2000) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded) <= maximum:
        return value
    return {"preview": encoded[:maximum], "truncated": True}


def build_shared_context(
    store: str | Path,
    *,
    profile_id: str,
    channel_id: str,
    subject_id: str = "",
    subject_ids: Iterable[str] | None = None,
    agent_id: str = "",
    workspace_id: str = "",
    limits: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Build bounded semantic context for an Agent ``before`` operation.

    Returned data never contains binary bytes or raw conversations.  Limits
    accept ``activities``, ``states``, ``spatial``, and ``characters``.
    """

    bounds = {"activities": 8, "states": 16, "spatial": 8, "characters": 12000}
    if limits:
        for key in bounds:
            if key in limits:
                bounds[key] = int(limits[key])
    for key in ("activities", "states", "spatial"):
        bounds[key] = max(0, min(bounds[key], 100))
    bounds["characters"] = max(1000, min(bounds["characters"], 100000))
    profile = _identifier(profile_id, "profile_id")
    channel = _identifier(channel_id, "channel_id")
    if agent_id or subject_id or subject_ids:
        accessible: set[str] = set()
        if agent_id:
            accessible.update(
                str(item["channel_id"])
                for item in list_channels(
                    store, profile_id=profile, member_type="agent", member_id=agent_id
                )
            )
        subject_members = {str(value).strip() for value in (subject_ids or []) if str(value).strip()}
        if subject_id:
            subject_members.add(str(subject_id).strip())
        for member in subject_members:
            accessible.update(
                str(item["channel_id"])
                for item in list_channels(
                    store, profile_id=profile, member_type="subject", member_id=member
                )
            )
        if channel not in accessible:
            raise PermissionError(f"Agent/subject is not a member of channel audience: {channel}")
    activities = list_activity_feed(
        store, profile_id=profile, channel_id=channel, subject_id=subject_id,
        subject_ids=subject_ids,
        limit=max(1, bounds["activities"]),
    ) if bounds["activities"] else []
    states = list_temporal_states(
        store, profile_id=profile, channel_id=channel, subject_id=subject_id,
        subject_ids=subject_ids,
        current_only=True, limit=max(1, bounds["states"]),
    ) if bounds["states"] else []
    from .spatial import list_spatial_observations

    spatial = list_spatial_observations(
        store, profile_id=profile, channel_id=channel, workspace_id=workspace_id,
        viewer_agent_id=agent_id, subject_id=subject_id, subject_ids=subject_ids,
        viewer_subject_ids=subject_ids, current_only=True, limit=max(1, bounds["spatial"]),
    ) if bounds["spatial"] else []
    context: dict[str, Any] = {
        "profile_id": profile,
        "channel_id": channel,
        "generated_at": _timestamp(),
        "activities": [
            {
                key: item.get(key) for key in (
                    "activity_id", "activity_kind", "title", "summary", "importance", "occurred_at",
                    "valid_until", "subject_id", "source_workspace_id", "source_agent_id",
                    "source_ref", "confidence",
                )
            }
            for item in activities
        ],
        "states": [
            {
                "state_id": item.get("state_id"), "subject_id": item.get("subject_id"),
                "state_key": item.get("state_key"), "summary": item.get("summary"),
                "value": _bounded(item.get("value")), "observed_at": item.get("observed_at"),
                "valid_until": item.get("valid_until"), "confidence": item.get("confidence"),
                "source_agent_id": item.get("source_agent_id"), "source_ref": item.get("source_ref"),
            }
            for item in states
        ],
        "spatial": [
            {
                "observation_id": item.get("observation_id"), "location_id": item.get("location_id"),
                "subject_id": item.get("subject_id"),
                "location_text": item.get("location_text"), "caption": str(item.get("caption") or "")[:1600],
                "ocr_text": str(item.get("ocr_text") or "")[:1200],
                "objects": list(item.get("objects") or [])[:20], "confidence": item.get("confidence"),
                "observed_at": item.get("observed_at"), "valid_until": item.get("valid_until"),
                "map_id": item.get("map_id"), "map_version": item.get("map_version"),
                "asset_uri": item.get("asset_uri"), "source_agent_id": item.get("source_agent_id"),
                "source_ref": item.get("source_ref"), "observation_kind": item.get("observation_kind"),
            }
            for item in spatial
        ],
        "truncated": False,
    }
    # Enforce one hard serialized-size boundary in addition to per-source limits.
    while len(json.dumps(context, ensure_ascii=False, default=str)) > bounds["characters"]:
        candidates = [(len(context[name]), name) for name in ("activities", "states", "spatial") if context[name]]
        if not candidates:
            break
        context[max(candidates)[1]].pop()
        context["truncated"] = True
    context["counts"] = {name: len(context[name]) for name in ("activities", "states", "spatial")}
    return context


__all__ = [
    "AUDIENCE_TYPES", "CHANNEL_TYPES", "MEMBER_TYPES", "ensure_audience", "grant_audience_member",
    "revoke_audience_member", "audience_ids_for_member", "ensure_channel", "list_channels",
    "publish_activity", "list_activity_feed", "publish_temporal_state", "list_temporal_states",
    "get_current_state", "expire_time_bounded", "build_shared_context",
]
