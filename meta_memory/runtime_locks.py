"""Fenced SQLite leases for singleton local maintenance work."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .legacy import bootstrap


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _until(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(1, seconds))).isoformat()


@dataclass(frozen=True)
class RuntimeLease:
    lock_name: str
    owner_id: str
    leased_until: str
    acquired: bool


def acquire(root: str | Path, lock_name: str, *, owner_id: str = "", lease_seconds: int = 600) -> RuntimeLease:
    bootstrap()
    from _common import open_db

    owner = owner_id or f"runtime:{uuid.uuid4()}"
    now, until = _now(), _until(lease_seconds)
    conn = open_db(Path(root))
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT owner_id,leased_until FROM runtime_locks WHERE lock_name=?", (lock_name,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO runtime_locks(lock_name,owner_id,leased_until,updated_at) VALUES(?, ?, ?, ?)", (lock_name, owner, until, now))
            conn.commit()
            return RuntimeLease(lock_name, owner, until, True)
        current_owner, expires = str(row[0]), str(row[1])
        if current_owner == owner or expires <= now:
            changed = conn.execute(
                "UPDATE runtime_locks SET owner_id=?,leased_until=?,updated_at=? WHERE lock_name=? AND (owner_id=? OR leased_until<=?)",
                (owner, until, now, lock_name, owner, now),
            ).rowcount
            conn.commit()
            return RuntimeLease(lock_name, owner, until, bool(changed))
        conn.commit()
        return RuntimeLease(lock_name, current_owner, expires, False)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def renew(root: str | Path, lease: RuntimeLease, *, lease_seconds: int = 600) -> bool:
    if not lease.acquired:
        return False
    bootstrap()
    from _common import open_db

    conn = open_db(Path(root))
    try:
        changed = conn.execute(
            "UPDATE runtime_locks SET leased_until=?,updated_at=? WHERE lock_name=? AND owner_id=?",
            (_until(lease_seconds), _now(), lease.lock_name, lease.owner_id),
        ).rowcount
        conn.commit()
        return bool(changed)
    finally:
        conn.close()


def release(root: str | Path, lease: RuntimeLease) -> bool:
    if not lease.acquired:
        return False
    bootstrap()
    from _common import open_db

    conn = open_db(Path(root))
    try:
        changed = conn.execute("DELETE FROM runtime_locks WHERE lock_name=? AND owner_id=?", (lease.lock_name, lease.owner_id)).rowcount
        conn.commit()
        return bool(changed)
    finally:
        conn.close()


def inspect(root: str | Path, lock_name: str) -> dict[str, object] | None:
    bootstrap()
    from _common import open_db

    conn = open_db(Path(root))
    try:
        row = conn.execute("SELECT owner_id,leased_until,updated_at FROM runtime_locks WHERE lock_name=?", (lock_name,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"lock_name": lock_name, "owner_id": str(row[0]), "leased_until": str(row[1]), "updated_at": str(row[2]), "active": str(row[1]) > _now()}
