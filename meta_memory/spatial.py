"""Filesystem-backed assets and searchable, versioned spatial memory."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Mapping

from .shared_memory import (
    MEMBER_TYPES,
    _channel,
    _confidence,
    _connection,
    _decoded,
    _enum,
    _identifier,
    _json,
    _optional_timestamp,
    _timestamp,
)


DEFAULT_MAX_ASSET_BYTES = 64 * 1024 * 1024
VISIBILITY_SCOPES = frozenset({"channel", "global", "workspace", "agent"})
ASSET_VISIBILITY_SCOPES = frozenset({"profile", "channel", "workspace", "agent"})


class AssetTooLargeError(ValueError):
    """Raised before an oversized upload can become an object."""


class AssetInUseError(RuntimeError):
    """Raised when a referenced asset is removed without ``force=True``."""


class AssetIntegrityError(RuntimeError):
    """Raised when stored bytes no longer match immutable metadata."""


def asset_uri(asset_id: str) -> str:
    return f"meta-memory://assets/{_identifier(asset_id, 'asset_id')}"


def _store_root(store: str | Path) -> Path:
    return Path(store).expanduser().resolve()


def _objects_root(store: str | Path) -> Path:
    root = _store_root(store) / "assets" / "objects"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _safe_original_name(value: str) -> str:
    # Metadata may retain a human hint, while the physical object name remains
    # opaque/content-addressed.  Never allow a supplied path through this API.
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if ord(char) >= 32 and char not in {"\x7f"}).strip(" .")
    return name[:255]


def _object_path(store: str | Path, relative: str) -> Path:
    root = _objects_root(store)
    candidate = (_store_root(store) / str(relative)).resolve()
    if not candidate.is_relative_to(root):
        raise AssetIntegrityError("asset object_path escapes the object store")
    return candidate


def _asset_result(row: sqlite3.Row | None, *, deduplicated: bool | None = None) -> dict[str, Any] | None:
    result = _decoded(row)
    if result is None:
        return None
    result["uri"] = asset_uri(str(result["asset_id"]))
    if "scope_original_name" in result:
        result["original_name"] = str(result.pop("scope_original_name") or result.get("original_name") or "")
    if "scope_media_type" in result:
        result["media_type"] = str(result.pop("scope_media_type") or result.get("media_type") or "application/octet-stream")
    if "scope_metadata" in result:
        result["metadata"] = result.pop("scope_metadata")
    if "access_visibility_scope" in result:
        result["access"] = {
            "visibility_scope": result.pop("access_visibility_scope"),
            "channel_id": result.pop("access_channel_id", ""),
            "workspace_id": result.pop("access_workspace_id", ""),
            "owner_agent_id": result.pop("access_owner_agent_id", ""),
        }
    if deduplicated is not None:
        result["deduplicated"] = deduplicated
    return result


def _active_asset(conn: sqlite3.Connection, *, profile_id: str, asset_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM binary_assets WHERE profile_id=? AND asset_id=? AND status='active'",
        (profile_id, asset_id),
    ).fetchone()
    if not row:
        raise KeyError(f"active asset not found: {asset_id}")
    return row


def _write_upload(
    store: str | Path,
    data: bytes | bytearray | memoryview | BinaryIO,
    *,
    max_bytes: int,
) -> tuple[Path, str, int]:
    maximum = int(max_bytes)
    if maximum <= 0:
        raise ValueError("max_bytes must be positive")
    objects = _objects_root(store)
    staging = objects.parent / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    temporary = staging / f"upload-{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("xb") as output:
            if isinstance(data, (bytes, bytearray, memoryview)):
                source: BinaryIO | None = None
                chunks: Iterable[bytes | bytearray | memoryview] = (data,)
            elif hasattr(data, "read"):
                source = data  # type: ignore[assignment]
                chunks = iter(())
            else:
                raise TypeError("data must be bytes or a binary stream")
            while True:
                if source is not None:
                    chunk = source.read(min(1024 * 1024, maximum + 1 - size))
                    if chunk in (b"", None):
                        break
                    current = chunk
                else:
                    try:
                        current = next(iter(chunks))
                    except StopIteration:
                        break
                    # The in-memory source contains exactly one chunk.
                    chunks = iter(())
                if not isinstance(current, (bytes, bytearray, memoryview)):
                    raise TypeError("asset stream must return bytes")
                raw = bytes(current)
                size += len(raw)
                if size > maximum:
                    raise AssetTooLargeError(f"asset exceeds max_bytes={maximum}")
                digest.update(raw)
                output.write(raw)
        return temporary, digest.hexdigest(), size
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def store_asset(
    store: str | Path,
    data: bytes | bytearray | memoryview | BinaryIO,
    *,
    profile_id: str,
    media_type: str = "application/octet-stream",
    original_name: str = "",
    metadata: Mapping[str, Any] | None = None,
    max_bytes: int = DEFAULT_MAX_ASSET_BYTES,
    visibility_scope: str = "profile",
    channel_id: str = "",
    workspace_id: str = "",
    owner_agent_id: str = "",
    source_subject_id: str = "",
    source_agent_id: str = "",
) -> dict[str, Any]:
    """Stream an asset to content-addressed storage with SHA-256 deduplication."""

    profile = _identifier(profile_id, "profile_id")
    media = _identifier(media_type, "media_type", maximum=255)
    visibility = _enum(visibility_scope, "visibility_scope", ASSET_VISIBILITY_SCOPES)
    channel = str(channel_id or "").strip()
    workspace = str(workspace_id or "").strip()
    owner = str(owner_agent_id or source_agent_id or "").strip()
    if visibility == "channel" and not channel:
        raise ValueError("channel_id is required for channel-visible assets")
    if visibility == "workspace" and not workspace:
        raise ValueError("workspace_id is required for workspace-visible assets")
    if visibility == "agent" and not owner:
        raise ValueError("owner_agent_id or source_agent_id is required for agent-visible assets")
    temporary, digest, size = _write_upload(store, data, max_bytes=max_bytes)
    relative = Path("assets") / "objects" / digest[:2] / digest
    destination = _store_root(store) / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    # A digest collision means byte-for-byte identical content for practical
    # operation.  Replacing a corrupted prior file also self-heals the store.
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    conn = _connection(store)
    deduplicated = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM binary_assets WHERE profile_id=? AND sha256=?",
            (profile, digest),
        ).fetchone()
        now = _timestamp()
        if row:
            deduplicated = str(row["status"]) == "active"
            asset_id = str(row["asset_id"])
            # The object identity and first-seen metadata are immutable.
            # Upload-specific names/media/metadata live on the scope binding.
            conn.execute(
                """UPDATE binary_assets SET byte_size=?,object_path=?,
                       status='active',updated_at=? WHERE asset_id=?""",
                (size, relative.as_posix(), now, asset_id),
            )
        else:
            asset_id = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO binary_assets(
                       asset_id,profile_id,sha256,media_type,byte_size,object_path,original_name,
                       metadata_json,status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?, 'active',?,?)""",
                (
                    asset_id, profile, digest, media, size, relative.as_posix(),
                    _safe_original_name(original_name), _json(metadata, object_only=True), now, now,
                ),
            )
        clean_name = _safe_original_name(original_name)
        scope_metadata = _json(metadata, object_only=True)
        conn.execute(
            """INSERT INTO binary_asset_scopes(
                   scope_id,asset_id,profile_id,visibility_scope,channel_id,workspace_id,owner_agent_id,
                   source_subject_id,source_agent_id,original_name,metadata_json,created_at,media_type
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(asset_id,visibility_scope,channel_id,workspace_id,owner_agent_id)
               DO UPDATE SET source_subject_id=excluded.source_subject_id,
                             source_agent_id=excluded.source_agent_id,
                             original_name=CASE WHEN excluded.original_name!='' THEN excluded.original_name ELSE binary_asset_scopes.original_name END,
                             metadata_json=CASE WHEN excluded.metadata_json!='{}' THEN excluded.metadata_json ELSE binary_asset_scopes.metadata_json END,
                             media_type=excluded.media_type""",
            (
                uuid.uuid4().hex, asset_id, profile, visibility, channel, workspace, owner,
                str(source_subject_id or "").strip(), str(source_agent_id or "").strip(),
                clean_name, scope_metadata, now, media,
            ),
        )
        row = conn.execute(
            """SELECT a.*,s.visibility_scope AS access_visibility_scope,
                      s.channel_id AS access_channel_id,s.workspace_id AS access_workspace_id,
                      s.owner_agent_id AS access_owner_agent_id,s.original_name AS scope_original_name,
                      s.metadata_json AS scope_metadata_json,s.media_type AS scope_media_type
               FROM binary_assets a JOIN binary_asset_scopes s ON s.asset_id=a.asset_id
               WHERE a.asset_id=? AND s.visibility_scope=? AND s.channel_id=?
                 AND s.workspace_id=? AND s.owner_agent_id=?""",
            (asset_id, visibility, channel, workspace, owner),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return _asset_result(row, deduplicated=deduplicated) or {}


def get_asset(
    store: str | Path,
    *,
    profile_id: str,
    asset_id: str,
    include_deleted: bool = False,
    enforce_visibility: bool = False,
    channel_id: str = "",
    workspace_id: str = "",
    viewer_agent_id: str = "",
) -> dict[str, Any] | None:
    profile = _identifier(profile_id, "profile_id")
    asset = _identifier(asset_id, "asset_id")
    conn = _connection(store)
    try:
        if enforce_visibility:
            channel = str(channel_id or "").strip()
            workspace = str(workspace_id or "").strip()
            viewer = str(viewer_agent_id or "").strip()
            row = conn.execute(
                """SELECT a.*,s.visibility_scope AS access_visibility_scope,
                          s.channel_id AS access_channel_id,s.workspace_id AS access_workspace_id,
                          s.owner_agent_id AS access_owner_agent_id,s.original_name AS scope_original_name,
                          s.metadata_json AS scope_metadata_json,s.media_type AS scope_media_type
                   FROM binary_assets a JOIN binary_asset_scopes s ON s.asset_id=a.asset_id
                   WHERE a.profile_id=? AND a.asset_id=?"""
                + ("" if include_deleted else " AND a.status='active'")
                + """ AND (s.visibility_scope='profile'
                         OR (s.visibility_scope='channel' AND s.channel_id=? AND ?!='')
                         OR (s.visibility_scope='workspace' AND s.workspace_id=? AND ?!='')
                         OR (s.visibility_scope='agent' AND s.owner_agent_id=? AND ?!=''))
                       ORDER BY CASE s.visibility_scope WHEN 'agent' THEN 0 WHEN 'channel' THEN 1
                                WHEN 'workspace' THEN 2 ELSE 3 END LIMIT 1""",
                (profile, asset, channel, channel, workspace, workspace, viewer, viewer),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM binary_assets WHERE profile_id=? AND asset_id=?"
                + ("" if include_deleted else " AND status='active'"),
                (profile, asset),
            ).fetchone()
        return _asset_result(row)
    finally:
        conn.close()


def list_assets(
    store: str | Path,
    *,
    profile_id: str,
    media_type: str = "",
    include_deleted: bool = False,
    limit: int = 100,
    enforce_visibility: bool = False,
    channel_id: str = "",
    workspace_id: str = "",
    viewer_agent_id: str = "",
) -> list[dict[str, Any]]:
    profile = _identifier(profile_id, "profile_id")
    clauses = ["a.profile_id=?"]
    values: list[Any] = [profile]
    if not include_deleted:
        clauses.append("a.status='active'")
    if media_type:
        clauses.append(("s.media_type=?" if enforce_visibility else "a.media_type=?"))
        values.append(str(media_type).strip())
    conn = _connection(store)
    try:
        if enforce_visibility:
            channel = str(channel_id or "").strip()
            workspace = str(workspace_id or "").strip()
            viewer = str(viewer_agent_id or "").strip()
            clauses.append(
                "(s.visibility_scope='profile' "
                "OR (s.visibility_scope='channel' AND s.channel_id=? AND ?!='') "
                "OR (s.visibility_scope='workspace' AND s.workspace_id=? AND ?!='') "
                "OR (s.visibility_scope='agent' AND s.owner_agent_id=? AND ?!=''))"
            )
            values.extend([channel, channel, workspace, workspace, viewer, viewer])
            rows = conn.execute(
                """WITH visible AS (
                       SELECT a.*,s.visibility_scope AS access_visibility_scope,
                              s.channel_id AS access_channel_id,s.workspace_id AS access_workspace_id,
                              s.owner_agent_id AS access_owner_agent_id,s.original_name AS scope_original_name,
                              s.metadata_json AS scope_metadata_json,s.media_type AS scope_media_type,
                              ROW_NUMBER() OVER(
                                PARTITION BY a.asset_id
                                ORDER BY CASE s.visibility_scope WHEN 'agent' THEN 0 WHEN 'channel' THEN 1
                                         WHEN 'workspace' THEN 2 ELSE 3 END
                              ) AS access_rank
                       FROM binary_assets a JOIN binary_asset_scopes s ON s.asset_id=a.asset_id
                       WHERE """
                + " AND ".join(clauses)
                + ") SELECT * FROM visible WHERE access_rank=1 ORDER BY created_at DESC LIMIT ?",
                [*values, max(1, min(int(limit), 1000))],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT a.* FROM binary_assets a WHERE " + " AND ".join(clauses)
                + " ORDER BY a.created_at DESC LIMIT ?",
                [*values, max(1, min(int(limit), 1000))],
            ).fetchall()
        return [_asset_result(row) or {} for row in rows]
    finally:
        conn.close()


def read_asset(
    store: str | Path,
    *,
    profile_id: str,
    asset_id: str,
    max_bytes: int | None = None,
    verify_hash: bool = True,
) -> bytes:
    path = asset_file(
        store,
        profile_id=profile_id,
        asset_id=asset_id,
        max_bytes=max_bytes,
        verify_hash=verify_hash,
    )
    return path.read_bytes()


def asset_file(
    store: str | Path,
    *,
    profile_id: str,
    asset_id: str,
    max_bytes: int | None = None,
    verify_hash: bool = True,
) -> Path:
    """Return a verified object path without loading the asset into memory."""

    profile = _identifier(profile_id, "profile_id")
    asset = _identifier(asset_id, "asset_id")
    conn = _connection(store)
    try:
        row = _active_asset(conn, profile_id=profile, asset_id=asset)
        size = int(row["byte_size"])
        if max_bytes is not None and size > int(max_bytes):
            raise AssetTooLargeError(f"asset exceeds read max_bytes={int(max_bytes)}")
        path = _object_path(store, str(row["object_path"]))
        if not path.is_file():
            exc = FileNotFoundError(path)
            raise AssetIntegrityError(f"asset bytes are missing: {asset}") from exc
        if path.stat().st_size != size:
            raise AssetIntegrityError(f"asset size mismatch: {asset}")
        if verify_hash:
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != str(row["sha256"]):
                raise AssetIntegrityError(f"asset hash mismatch: {asset}")
        return path
    finally:
        conn.close()


def remove_asset(
    store: str | Path,
    *,
    profile_id: str,
    asset_id: str,
    force: bool = False,
) -> dict[str, Any]:
    profile = _identifier(profile_id, "profile_id")
    asset = _identifier(asset_id, "asset_id")
    conn = _connection(store)
    object_path = ""
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _active_asset(conn, profile_id=profile, asset_id=asset)
        map_refs = int(conn.execute("SELECT COUNT(*) FROM spatial_maps WHERE asset_id=?", (asset,)).fetchone()[0])
        observation_refs = int(conn.execute("SELECT COUNT(*) FROM spatial_observations WHERE asset_id=?", (asset,)).fetchone()[0])
        references = map_refs + observation_refs
        if references and not force:
            raise AssetInUseError(f"asset is referenced by {references} spatial records")
        object_path = str(row["object_path"])
        now = _timestamp()
        conn.execute("UPDATE binary_assets SET status='deleted',updated_at=? WHERE asset_id=?", (now, asset))
        # Access bindings are grants for this active asset incarnation, not
        # immutable history.  If identical bytes are uploaded again later,
        # only the new upload's explicit scope may become visible.
        conn.execute("DELETE FROM binary_asset_scopes WHERE asset_id=?", (asset,))
        active_twins = int(
            conn.execute(
                "SELECT COUNT(*) FROM binary_assets WHERE object_path=? AND status='active' AND asset_id!=?",
                (object_path, asset),
            ).fetchone()[0]
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    removed_bytes = False
    if not active_twins:
        path = _object_path(store, object_path)
        removed_bytes = path.exists()
        path.unlink(missing_ok=True)
    return {
        "asset_id": asset,
        "status": "deleted",
        "references": {"maps": map_refs, "observations": observation_refs},
        "removed_bytes": removed_bytes,
        "forced": bool(force),
    }


def _map_result(row: sqlite3.Row | None, *, deduplicated: bool | None = None) -> dict[str, Any] | None:
    result = _decoded(row)
    if result is None:
        return None
    if result.get("asset_id") and result.get("asset_status") == "active":
        result["asset_uri"] = asset_uri(str(result["asset_id"]))
    else:
        result["asset_uri"] = None
    result.pop("asset_status", None)
    if deduplicated is not None:
        result["deduplicated"] = deduplicated
    return result


def create_map_version(
    store: str | Path,
    *,
    profile_id: str,
    channel_id: str,
    map_id: str,
    coordinate_frame: str,
    version: int | None = None,
    name: str = "",
    asset_id: str = "",
    source_workspace_id: str = "",
    source_agent_id: str = "",
    captured_at: str | datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
    idempotency_key: str = "",
) -> dict[str, Any]:
    profile = _identifier(profile_id, "profile_id")
    channel = _identifier(channel_id, "channel_id")
    stable_map = _identifier(map_id, "map_id", maximum=128)
    frame = _identifier(coordinate_frame, "coordinate_frame", maximum=128)
    asset = str(asset_id or "").strip()
    source_agent = str(source_agent_id or "").strip()
    idem = str(idempotency_key or "").strip()
    if idem and not source_agent:
        raise ValueError("source_agent_id is required with idempotency_key")
    conn = _connection(store)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _channel(conn, profile_id=profile, channel_id=channel)
        if asset:
            _active_asset(conn, profile_id=profile, asset_id=asset)
        if idem:
            existing = conn.execute(
                """SELECT m.*,a.status AS asset_status FROM spatial_maps m
                   LEFT JOIN binary_assets a ON a.asset_id=m.asset_id
                   WHERE m.profile_id=? AND m.source_agent_id=? AND m.idempotency_key=?""",
                (profile, source_agent, idem),
            ).fetchone()
            if existing:
                conn.commit()
                return _map_result(existing, deduplicated=True) or {}
        previous = conn.execute(
            "SELECT * FROM spatial_maps WHERE profile_id=? AND map_id=? ORDER BY version DESC LIMIT 1",
            (profile, stable_map),
        ).fetchone()
        if previous and str(previous["channel_id"] or "") != channel:
            raise ValueError(
                "map_id is already owned by another channel; choose a new stable map_id"
            )
        newest = int(previous["version"]) if previous else 0
        selected_version = newest + 1 if version is None else int(version)
        if selected_version <= 0:
            raise ValueError("map version must be positive")
        if selected_version <= newest:
            raise ValueError(f"map version must be newer than {newest}")
        map_version_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO spatial_maps(
                   map_version_id,profile_id,channel_id,map_id,version,name,coordinate_frame,asset_id,
                   source_workspace_id,source_agent_id,captured_at,previous_version_id,idempotency_key,
                   metadata_json,status,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'active',?)""",
            (
                map_version_id, profile, channel, stable_map, selected_version, str(name).strip(), frame,
                asset or None, str(source_workspace_id).strip(), source_agent, _timestamp(captured_at),
                str(previous["map_version_id"]) if previous else None, idem or None,
                _json(metadata, object_only=True), _timestamp(),
            ),
        )
        row = conn.execute(
            """SELECT m.*,a.status AS asset_status FROM spatial_maps m
               LEFT JOIN binary_assets a ON a.asset_id=m.asset_id WHERE m.map_version_id=?""",
            (map_version_id,),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return _map_result(row, deduplicated=False) or {}


def get_map(
    store: str | Path,
    *,
    profile_id: str,
    map_id: str = "",
    version: int | None = None,
    map_version_id: str = "",
) -> dict[str, Any] | None:
    profile = _identifier(profile_id, "profile_id")
    clauses = ["m.profile_id=?", "m.status='active'"]
    values: list[Any] = [profile]
    if map_version_id:
        clauses.append("m.map_version_id=?")
        values.append(_identifier(map_version_id, "map_version_id"))
    else:
        clauses.append("m.map_id=?")
        values.append(_identifier(map_id, "map_id", maximum=128))
        if version is not None:
            clauses.append("m.version=?")
            values.append(int(version))
    conn = _connection(store)
    try:
        row = conn.execute(
            """SELECT m.*,a.status AS asset_status FROM spatial_maps m
               LEFT JOIN binary_assets a ON a.asset_id=m.asset_id WHERE """
            + " AND ".join(clauses)
            + " ORDER BY m.version DESC LIMIT 1",
            values,
        ).fetchone()
        return _map_result(row)
    finally:
        conn.close()


def list_maps(
    store: str | Path,
    *,
    profile_id: str,
    channel_id: str = "",
    latest_only: bool = True,
    limit: int = 100,
) -> list[dict[str, Any]]:
    profile = _identifier(profile_id, "profile_id")
    clauses = ["m.profile_id=?", "m.status='active'"]
    values: list[Any] = [profile]
    if channel_id:
        clauses.append("m.channel_id=?")
        values.append(_identifier(channel_id, "channel_id"))
    if latest_only:
        clauses.append(
            "m.version=(SELECT MAX(m2.version) FROM spatial_maps m2 "
            "WHERE m2.profile_id=m.profile_id AND m2.channel_id=m.channel_id "
            "AND m2.map_id=m.map_id AND m2.status='active')"
        )
    conn = _connection(store)
    try:
        rows = conn.execute(
            """SELECT m.*,a.status AS asset_status FROM spatial_maps m
               LEFT JOIN binary_assets a ON a.asset_id=m.asset_id WHERE """
            + " AND ".join(clauses)
            + " ORDER BY m.map_id,m.version DESC LIMIT ?",
            [*values, max(1, min(int(limit), 1000))],
        ).fetchall()
        return [_map_result(row) or {} for row in rows]
    finally:
        conn.close()


def _observation_result(row: sqlite3.Row | None) -> dict[str, Any] | None:
    result = _decoded(row)
    if result is None:
        return None
    if result.get("asset_id") and result.get("asset_status") == "active":
        result["asset_uri"] = asset_uri(str(result["asset_id"]))
    else:
        result["asset_uri"] = None
    result.pop("asset_status", None)
    return result


def _resolve_map(
    conn: sqlite3.Connection,
    *,
    profile_id: str,
    channel_id: str,
    map_version_id: str,
    map_id: str,
    map_version: int | None,
) -> sqlite3.Row | None:
    if map_version_id:
        row = conn.execute(
            "SELECT * FROM spatial_maps WHERE profile_id=? AND channel_id=? AND map_version_id=? AND status='active'",
            (profile_id, channel_id, map_version_id),
        ).fetchone()
    elif map_id:
        sql = "SELECT * FROM spatial_maps WHERE profile_id=? AND channel_id=? AND map_id=? AND status='active'"
        values: list[Any] = [profile_id, channel_id, map_id]
        if map_version is not None:
            sql += " AND version=?"
            values.append(int(map_version))
        row = conn.execute(sql + " ORDER BY version DESC LIMIT 1", values).fetchone()
    else:
        return None
    if not row:
        raise KeyError("active map version not found in channel")
    return row


def record_spatial_observation(
    store: str | Path,
    *,
    profile_id: str,
    channel_id: str,
    workspace_id: str = "",
    subject_id: str = "",
    source_agent_id: str = "",
    source_ref: str = "",
    observation_kind: str = "spatial_observation",
    owner_agent_id: str = "",
    map_version_id: str = "",
    map_id: str = "",
    map_version: int | None = None,
    asset_id: str = "",
    location_id: str = "",
    location_text: str = "",
    caption: str = "",
    ocr_text: str = "",
    objects: Iterable[Mapping[str, Any] | str] | None = None,
    confidence: float | None = None,
    observed_at: str | datetime | None = None,
    valid_until: str | datetime | None = None,
    visibility_scope: str = "channel",
    supersedes_observation_id: str = "",
    idempotency_key: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    profile = _identifier(profile_id, "profile_id")
    channel = _identifier(channel_id, "channel_id")
    source_agent = str(source_agent_id or "").strip()
    kind = _identifier(observation_kind or "spatial_observation", "observation_kind", maximum=80)
    visibility = _enum(visibility_scope, "visibility_scope", VISIBILITY_SCOPES)
    owner = str(owner_agent_id or (source_agent if visibility == "agent" else "")).strip()
    if visibility == "agent" and not owner:
        raise ValueError("owner_agent_id or source_agent_id is required for agent visibility")
    object_list = list(objects or [])
    object_json = _json(object_list)
    if not isinstance(json.loads(object_json), list):
        raise ValueError("objects must be a JSON array")
    asset = str(asset_id or "").strip()
    location = str(location_text or "").strip()
    caption_text = str(caption or "").strip()
    ocr = str(ocr_text or "").strip()
    if not any((asset, map_version_id, map_id, location, caption_text, ocr, object_list)):
        raise ValueError("observation must contain an asset, map, location, caption, OCR, or objects")
    idem = str(idempotency_key or "").strip()
    if idem and not source_agent:
        raise ValueError("source_agent_id is required with idempotency_key")
    observed = _timestamp(observed_at)
    until = _optional_timestamp(valid_until)
    if until and until <= observed:
        raise ValueError("valid_until must be later than observed_at")
    conn = _connection(store)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _channel(conn, profile_id=profile, channel_id=channel)
        if idem:
            existing = conn.execute(
                """SELECT o.*,m.map_id,m.version AS map_version,m.coordinate_frame,a.status AS asset_status
                   FROM spatial_observations o
                   LEFT JOIN spatial_maps m ON m.map_version_id=o.map_version_id
                   LEFT JOIN binary_assets a ON a.asset_id=o.asset_id
                   WHERE o.profile_id=? AND o.source_agent_id=? AND o.idempotency_key=?""",
                (profile, source_agent, idem),
            ).fetchone()
            if existing:
                conn.commit()
                result = _observation_result(existing) or {}
                result["deduplicated"] = True
                return result
        map_row = _resolve_map(
            conn, profile_id=profile, channel_id=channel, map_version_id=str(map_version_id or "").strip(),
            map_id=str(map_id or "").strip(), map_version=map_version,
        )
        if asset:
            _active_asset(conn, profile_id=profile, asset_id=asset)
        supersedes = str(supersedes_observation_id or "").strip() or None
        if supersedes:
            previous = conn.execute(
                """SELECT 1 FROM spatial_observations
                   WHERE observation_id=? AND profile_id=? AND channel_id=? AND status='active'""",
                (supersedes, profile, channel),
            ).fetchone()
            if not previous:
                raise KeyError(f"active observation to supersede not found in channel: {supersedes}")
        observation_id = uuid.uuid4().hex
        now = _timestamp()
        status = "expired" if until and until <= now else "active"
        search_text = "\n".join(
            part for part in (
                str(location_id or "").strip(), location, caption_text, ocr,
                json.dumps(object_list, ensure_ascii=False, sort_keys=True),
            ) if part
        )
        conn.execute(
            """INSERT INTO spatial_observations(
                   observation_id,profile_id,channel_id,workspace_id,subject_id,source_agent_id,owner_agent_id,
                   map_version_id,asset_id,location_id,location_text,caption,ocr_text,objects_json,search_text,
                   confidence,observed_at,valid_until,visibility_scope,status,supersedes_observation_id,
                   idempotency_key,metadata_json,created_at,updated_at,observation_kind,source_ref
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                observation_id, profile, channel, str(workspace_id).strip(), str(subject_id).strip(), source_agent,
                owner, str(map_row["map_version_id"]) if map_row else None, asset or None,
                str(location_id).strip(), location, caption_text, ocr, object_json, search_text,
                _confidence(confidence), observed, until, visibility, status, supersedes, idem or None,
                _json(metadata, object_only=True), now, now, kind, str(source_ref or "").strip(),
            ),
        )
        if supersedes and status == "active":
            conn.execute(
                """UPDATE spatial_observations SET status='superseded',superseded_by_observation_id=?,updated_at=?
                   WHERE observation_id=?""",
                (observation_id, now, supersedes),
            )
        row = conn.execute(
            """SELECT o.*,m.map_id,m.version AS map_version,m.coordinate_frame,a.status AS asset_status
               FROM spatial_observations o
               LEFT JOIN spatial_maps m ON m.map_version_id=o.map_version_id
               LEFT JOIN binary_assets a ON a.asset_id=o.asset_id
               WHERE o.observation_id=?""",
            (observation_id,),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    result = _observation_result(row) or {}
    result["deduplicated"] = False
    return result


def _observation_query(
    store: str | Path,
    *,
    profile_id: str,
    channel_id: str = "",
    map_id: str = "",
    map_version: int | None = None,
    location_id: str = "",
    source_agent_id: str = "",
    subject_id: str = "",
    subject_ids: Iterable[str] | None = None,
    workspace_id: str = "",
    viewer_agent_id: str = "",
    viewer_subject_ids: Iterable[str] | None = None,
    query: str = "",
    current_only: bool = True,
    now: str | datetime | None = None,
    observation_id: str = "",
    limit: int = 100,
) -> list[dict[str, Any]]:
    profile = _identifier(profile_id, "profile_id")
    clauses = ["o.profile_id=?"]
    values: list[Any] = [profile]
    if current_only:
        clauses.extend(["o.status='active'", "(o.valid_until IS NULL OR o.valid_until>?)"])
        values.append(_timestamp(now))
    if observation_id:
        clauses.append("o.observation_id=?")
        values.append(_identifier(observation_id, "observation_id"))
    if channel_id:
        clauses.append("o.channel_id=?")
        values.append(_identifier(channel_id, "channel_id"))
    if map_id:
        clauses.append("m.map_id=?")
        values.append(_identifier(map_id, "map_id", maximum=128))
    if map_version is not None:
        clauses.append("m.version=?")
        values.append(int(map_version))
    if location_id:
        clauses.append("o.location_id=?")
        values.append(str(location_id).strip())
    if source_agent_id:
        clauses.append("o.source_agent_id=?")
        values.append(str(source_agent_id).strip())
    if subject_id:
        clauses.append("(o.subject_id='' OR o.subject_id=?)")
        values.append(str(subject_id).strip())
    elif subject_ids:
        allowed_subjects = sorted({str(value).strip() for value in subject_ids if str(value).strip()})
        if allowed_subjects:
            clauses.append(
                "(o.subject_id='' OR o.subject_id IN ("
                + ",".join("?" for _ in allowed_subjects)
                + "))"
            )
            values.extend(allowed_subjects)
    viewer = str(viewer_agent_id or "").strip()
    workspace = str(workspace_id or "").strip()
    member_subjects = sorted(
        {str(value).strip() for value in (viewer_subject_ids or []) if str(value).strip()}
    )
    if viewer or member_subjects:
        # Audience membership gates the channel; visibility then narrows a
        # record to channel/global, the matching workspace, or its owner.
        membership = ["(am.member_type='profile' AND am.member_id=?)"]
        membership_values: list[Any] = [profile]
        if viewer:
            membership.append("(am.member_type='agent' AND am.member_id=?)")
            membership_values.append(viewer)
        if member_subjects:
            membership.append(
                "(am.member_type='subject' AND am.member_id IN ("
                + ",".join("?" for _ in member_subjects)
                + "))"
            )
            membership_values.extend(member_subjects)
        clauses.append(
            "EXISTS(SELECT 1 FROM memory_audience_members am WHERE am.audience_id=c.audience_id AND ("
            + " OR ".join(membership)
            + "))"
        )
        values.extend(membership_values)
        clauses.append(
            "(o.visibility_scope IN ('channel','global') "
            "OR (o.visibility_scope='workspace' AND o.workspace_id=?) "
            "OR (o.visibility_scope='agent' AND o.owner_agent_id=?))"
        )
        values.extend([workspace, viewer])
    elif workspace:
        clauses.append("(o.visibility_scope!='workspace' OR o.workspace_id=?)")
        values.append(workspace)
    for token in str(query or "").split()[:10]:
        escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("o.search_text LIKE ? ESCAPE '\\'")
        values.append(f"%{escaped}%")
    conn = _connection(store)
    try:
        rows = conn.execute(
            """SELECT o.*,m.map_id,m.version AS map_version,m.coordinate_frame,a.status AS asset_status
               FROM spatial_observations o
               JOIN memory_channels c ON c.channel_id=o.channel_id
               LEFT JOIN spatial_maps m ON m.map_version_id=o.map_version_id
               LEFT JOIN binary_assets a ON a.asset_id=o.asset_id
               WHERE """
            + " AND ".join(clauses)
            + " ORDER BY o.observed_at DESC,o.created_at DESC LIMIT ?",
            [*values, max(1, min(int(limit), 1000))],
        ).fetchall()
        return [_observation_result(row) or {} for row in rows]
    finally:
        conn.close()


def get_spatial_observation(
    store: str | Path,
    *,
    profile_id: str,
    observation_id: str,
    include_history: bool = True,
    channel_id: str = "",
    subject_id: str = "",
    subject_ids: Iterable[str] | None = None,
    workspace_id: str = "",
    viewer_agent_id: str = "",
    viewer_subject_ids: Iterable[str] | None = None,
) -> dict[str, Any] | None:
    rows = _observation_query(
        store, profile_id=profile_id, observation_id=observation_id,
        channel_id=channel_id, subject_id=subject_id, subject_ids=subject_ids,
        workspace_id=workspace_id, viewer_agent_id=viewer_agent_id,
        viewer_subject_ids=viewer_subject_ids,
        current_only=not include_history, limit=1,
    )
    return rows[0] if rows else None


def list_spatial_observations(
    store: str | Path,
    *,
    profile_id: str,
    channel_id: str = "",
    map_id: str = "",
    map_version: int | None = None,
    location_id: str = "",
    source_agent_id: str = "",
    subject_id: str = "",
    subject_ids: Iterable[str] | None = None,
    workspace_id: str = "",
    viewer_agent_id: str = "",
    viewer_subject_ids: Iterable[str] | None = None,
    current_only: bool = True,
    now: str | datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return _observation_query(
        store, profile_id=profile_id, channel_id=channel_id, map_id=map_id,
        map_version=map_version, location_id=location_id, source_agent_id=source_agent_id,
        subject_id=subject_id, subject_ids=subject_ids, workspace_id=workspace_id,
        viewer_agent_id=viewer_agent_id, viewer_subject_ids=viewer_subject_ids,
        current_only=current_only, now=now, limit=limit,
    )


def search_spatial_observations(
    store: str | Path,
    *,
    profile_id: str,
    query: str,
    channel_id: str = "",
    map_id: str = "",
    workspace_id: str = "",
    viewer_agent_id: str = "",
    subject_id: str = "",
    subject_ids: Iterable[str] | None = None,
    viewer_subject_ids: Iterable[str] | None = None,
    current_only: bool = True,
    now: str | datetime | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    text = str(query or "").strip()
    if not text:
        raise ValueError("query is required")
    return _observation_query(
        store, profile_id=profile_id, channel_id=channel_id, map_id=map_id,
        workspace_id=workspace_id, viewer_agent_id=viewer_agent_id,
        subject_id=subject_id, subject_ids=subject_ids,
        viewer_subject_ids=viewer_subject_ids, query=text,
        current_only=current_only, now=now, limit=limit,
    )


__all__ = [
    "DEFAULT_MAX_ASSET_BYTES", "VISIBILITY_SCOPES", "AssetTooLargeError", "AssetInUseError",
    "AssetIntegrityError", "asset_uri", "store_asset", "get_asset", "list_assets", "asset_file", "read_asset",
    "remove_asset", "create_map_version", "get_map", "list_maps", "record_spatial_observation",
    "get_spatial_observation", "list_spatial_observations", "search_spatial_observations",
]
