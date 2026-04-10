#!/usr/bin/env python3
from __future__ import annotations

import math

from _common import emit, open_db, parse_args, store_root


def main() -> None:
    args = parse_args("Recompute lightweight memory rank scores.")
    root = store_root(args.store)
    conn = open_db(root)
    rows = conn.execute(
        "SELECT path, COALESCE(hit_count, 0), COALESCE(confidence, 0.0) FROM scores"
    ).fetchall()
    updated = 0
    for path, hit_count, confidence in rows:
        score = round(math.log1p(hit_count) + float(confidence), 4)
        conn.execute(
            "UPDATE scores SET rank_score = ? WHERE path = ?",
            (score, path),
        )
        updated += 1
    conn.commit()
    conn.close()
    emit({"status": "ok", "updated": updated, "store": str(root)})


if __name__ == "__main__":
    main()
