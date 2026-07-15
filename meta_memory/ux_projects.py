"""Project binding and inspection commands for the public CLI."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import AppConfig, save_config, slug
from .legacy import bootstrap
from .project_detection import _remote_binding_key, project_root, resolve_project


def _ready(config: AppConfig):
    bootstrap()
    from _common import ensure_store_ready, open_db

    ensure_store_ready(Path(config.store))
    return open_db(Path(config.store))


def project_current(config: AppConfig, *, project_name: str = "auto", start: str | Path | None = None) -> dict[str, Any]:
    context = resolve_project(config, project_name, start)
    root = Path(context.root).resolve()
    path_key = str(root)
    remote_key = _remote_binding_key(root)
    bindings = []
    if path_key in config.projects:
        bindings.append({"kind": "path", "key": path_key, "project": config.projects[path_key]})
    if remote_key and remote_key in config.projects:
        bindings.append({"kind": "remote", "key": remote_key, "project": config.projects[remote_key]})
    return {
        "status": "ok", "name": context.name, "project": context.project_id,
        "workspace_id": context.workspace_id, "root": str(root), "bound": bool(bindings), "bindings": bindings,
    }


def project_list(config: AppConfig) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for key, value in sorted(config.projects.items()):
        grouped.setdefault(str(value), []).append(
            {"kind": "remote" if str(key).startswith("remote:") else "path", "binding": str(key)}
        )
    projects = [
        {"project": project_id, "workspace_id": f"project:{project_id}", "bindings": bindings}
        for project_id, bindings in sorted(grouped.items())
    ]
    return {"status": "ok", "projects": projects, "returned": len(projects), "config": str(config.path)}


def project_unbind(
    config: AppConfig,
    *,
    start: str | Path | None = None,
    all_bindings: bool = False,
) -> dict[str, Any]:
    root = project_root(start)
    targets = set(config.projects) if all_bindings else {str(root)}
    if not all_bindings:
        remote = _remote_binding_key(root)
        if remote:
            targets.add(remote)
    removed = {key: config.projects.pop(key) for key in list(targets) if key in config.projects}
    save_config(config)
    return {
        "status": "ok", "root": str(root), "removed": removed,
        "remaining": len(config.projects), "config": str(config.path),
    }


def _workspace_tables(conn) -> list[tuple[str, set[str]]]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    result: list[tuple[str, set[str]]] = []
    for row in rows:
        name = str(row[0])
        # SQLite table names come from our local schema, but quote defensively
        # since this is a general compatibility pass across migration versions.
        escaped = name.replace('"', '""')
        columns = {str(item[1]) for item in conn.execute(f'PRAGMA table_info("{escaped}")')}
        if "workspace_id" in columns:
            result.append((name, columns))
    return result


def _migrate_workspace(config: AppConfig, *, old_id: str, new_id: str) -> dict[str, int]:
    if old_id == new_id:
        return {}
    old_workspace, new_workspace = f"project:{old_id}", f"project:{new_id}"
    conn = _ready(config)
    changed: dict[str, int] = {}
    try:
        tables = _workspace_tables(conn)
        # Failing early prevents a half-renamed project if the destination is
        # already populated under a different explicit binding.
        collisions: list[str] = []
        for table, columns in tables:
            escaped = table.replace('"', '""')
            clauses = ["workspace_id=?"]
            params: list[object] = [new_workspace]
            if "profile_id" in columns:
                clauses.append("profile_id=?")
                params.append(config.profile_id)
            if "subject_id" in columns:
                clauses.append("subject_id=?")
                params.append(config.subject_id)
            if conn.execute(f'SELECT 1 FROM "{escaped}" WHERE ' + " AND ".join(clauses) + " LIMIT 1", params).fetchone():
                collisions.append(table)
        if collisions:
            raise ValueError(
                "Cannot rename this project because the destination already has data in: " + ", ".join(collisions[:8])
            )
        conn.execute("BEGIN IMMEDIATE")
        for table, columns in tables:
            escaped = table.replace('"', '""')
            clauses = ["workspace_id=?"]
            params: list[object] = [old_workspace]
            if "profile_id" in columns:
                clauses.append("profile_id=?")
                params.append(config.profile_id)
            if "subject_id" in columns:
                clauses.append("subject_id=?")
                params.append(config.subject_id)
            count = conn.execute(
                f'UPDATE "{escaped}" SET workspace_id=? WHERE ' + " AND ".join(clauses),
                (new_workspace, *params),
            ).rowcount
            if count:
                changed[table] = int(count)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return changed


def project_rename(config: AppConfig, *, old: str, new: str) -> dict[str, Any]:
    old_id, new_id = slug(old, config.default_project), slug(new, config.default_project)
    if old_id == new_id:
        return {"status": "ok", "old": old_id, "new": new_id, "idempotent": True, "migrated": {}}
    matching = [key for key, value in config.projects.items() if str(value) == old_id]
    if not matching:
        raise ValueError("Project is not explicitly bound. Run `meta-memory project set <name>` before renaming it.")
    changed = _migrate_workspace(config, old_id=old_id, new_id=new_id)
    for key in matching:
        config.projects[key] = new_id
    if config.default_project == old_id:
        config.default_project = new_id
    save_config(config)
    return {
        "status": "ok", "old": old_id, "new": new_id, "workspace_id": f"project:{new_id}",
        "bindings_updated": len(matching), "migrated": changed, "config": str(config.path),
    }


def project_stats(
    config: AppConfig,
    *,
    project_name: str = "auto",
    start: str | Path | None = None,
) -> dict[str, Any]:
    context = resolve_project(config, project_name, start)
    conn = _ready(config)
    try:
        scope = (config.profile_id, context.workspace_id, config.subject_id)
        claims = {
            str(row[0] or "unknown"): int(row[1])
            for row in conn.execute(
                "SELECT status,COUNT(*) FROM claims WHERE profile_id=? AND workspace_id=? AND subject_id=? GROUP BY status",
                scope,
            )
        }
        values = {
            "claims": claims,
            "raw_events": int(conn.execute("SELECT COUNT(*) FROM raw_events WHERE profile_id=? AND workspace_id=? AND subject_id=?", scope).fetchone()[0]),
            "sessions": int(conn.execute("SELECT COUNT(*) FROM sessions WHERE profile_id=? AND workspace_id=? AND subject_id=?", scope).fetchone()[0]),
            "completed_sessions": int(conn.execute("SELECT COUNT(*) FROM session_cards WHERE profile_id=? AND workspace_id=? AND subject_id=? AND completed_turn_count>0", scope).fetchone()[0]),
            "resources": int(conn.execute("SELECT COUNT(*) FROM resource_imports WHERE profile_id=? AND workspace_id=? AND subject_id=?", scope).fetchone()[0]),
            "inbox": int(conn.execute("SELECT COUNT(*) FROM write_proposals WHERE profile_id=? AND workspace_id=? AND subject_id=? AND status IN ('pending','needs_clarification')", scope).fetchone()[0]),
        }
    finally:
        conn.close()
    return {"status": "ok", "project": context.project_id, "workspace_id": context.workspace_id, "stats": values}
