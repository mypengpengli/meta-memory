#!/usr/bin/env python3
"""Backfill 2.1 session scope keys and merge legacy/new duplicate sessions."""
from __future__ import annotations

import argparse

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root
from session_archive import scope_key


def backfill(root, *, dry_run: bool = False) -> dict[str, object]:
    conn = open_db(root)
    rows = conn.execute("SELECT session_id, external_session_id, subject_id, profile_id, workspace_id, started_at, last_active_at FROM sessions WHERE scope_key IS NULL OR scope_key='' ORDER BY started_at").fetchall()
    changed = merged = 0
    for legacy_id, external, subject, profile, workspace, started, active in rows:
        scope = scope_key(workspace_id=str(workspace or 'default'), profile_id=str(profile or 'default'), subject_id=str(subject), session_id=str(external or legacy_id))
        duplicate = conn.execute("SELECT session_id, started_at, last_active_at FROM sessions WHERE scope_key=? AND session_id!=?", (scope, legacy_id)).fetchone()
        if duplicate:
            target = str(duplicate[0])
            if not dry_run:
                conn.execute("UPDATE session_messages SET session_id=? WHERE session_id=?", (target, legacy_id))
                conn.execute("UPDATE sessions SET parent_session_id=? WHERE parent_session_id=?", (target, legacy_id))
                conn.execute("UPDATE hot_snapshots SET session_id=? WHERE session_id=?", (target, legacy_id))
                conn.execute("UPDATE sessions SET started_at=MIN(started_at, ?), last_active_at=MAX(last_active_at, ?) WHERE session_id=?", (started, active, target))
                conn.execute("DELETE FROM sessions WHERE session_id=?", (legacy_id,))
            merged += 1
        else:
            if not dry_run:
                conn.execute("UPDATE sessions SET external_session_id=?, scope_key=? WHERE session_id=?", (str(external or legacy_id), scope, legacy_id))
            changed += 1
    if not dry_run: conn.commit()
    conn.close()
    return {"status": "ok", "backfilled": changed, "merged": merged, "dry_run": dry_run}


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill scope keys after a 2.1 to 2.2 upgrade.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP); parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(); emit(backfill(store_root(args.store), dry_run=args.dry_run))


if __name__ == "__main__": main()
