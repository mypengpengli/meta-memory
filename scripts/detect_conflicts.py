#!/usr/bin/env python3
"""Find structured conflicts without treating them as automatic deletions."""
from __future__ import annotations

import argparse

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root


def find_conflict_candidates(root, *, subject_id: str = "") -> list[dict[str, object]]:
    conn = open_db(root)
    clause, params = "WHERE status='active' AND predicate!=''", []
    if subject_id:
        clause += " AND subject_id=?"; params.append(subject_id)
    rows = conn.execute(f"SELECT id, subject_id, predicate, subject_text, object_text, title FROM claims {clause}", params).fetchall()
    groups: dict[tuple[str, str, str], list[tuple]] = {}
    for row in rows:
        groups.setdefault((str(row[1]), str(row[2]), str(row[3]).casefold()), []).append(row)
    conflicts: list[dict[str, object]] = []
    for key, claims in groups.items():
        objects = {str(row[4]).casefold() for row in claims if str(row[4]).strip()}
        if len(objects) > 1:
            conflicts.append({"subject_id": key[0], "predicate": key[1], "subject_text": key[2], "claim_ids": [str(row[0]) for row in claims], "objects": sorted(objects), "status": "review"})
    conn.close()
    return conflicts


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan active structured claims for potential contradictions.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP); parser.add_argument("--subject-id")
    args = parser.parse_args(); emit({"status": "ok", "conflicts": find_conflict_candidates(store_root(args.store), subject_id=args.subject_id or "")})


if __name__ == "__main__": main()
