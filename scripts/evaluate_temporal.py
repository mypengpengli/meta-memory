#!/usr/bin/env python3
"""Verify that temporal queries include only currently valid claims."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate temporal claim visibility.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--cases-file", required=True)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    cases = json.loads(Path(args.cases_file).read_text(encoding="utf-8-sig"))
    if not isinstance(cases, list):
        raise SystemExit("Cases file must be a JSON array")
    if args.strict and not cases:
        raise SystemExit("Strict evaluation requires at least one temporal case.")
    root = store_root(args.store)
    conn = open_db(root)
    results: list[dict[str, object]] = []
    for case in cases:
        rows = conn.execute(
            """SELECT id, title FROM claims WHERE subject_id=? AND status NOT IN ('superseded','corrected')
               AND (valid_from IS NULL OR valid_from='' OR valid_from<=?)
               AND (valid_to IS NULL OR valid_to='' OR valid_to>?)""",
            (case["subject_id"], case["at"], case["at"]),
        ).fetchall()
        titles = [str(row[1]) for row in rows]
        required = list(case.get("must_include", []))
        forbidden = list(case.get("must_not_include", []))
        missing = [value for value in required if value not in titles]
        unexpected = [value for value in forbidden if value in titles]
        results.append({"name": case.get("name", "unnamed"), "selected_titles": titles, "missing": missing, "unexpected": unexpected, "passed": not missing and not unexpected})
    conn.close()
    failed = [item for item in results if not item["passed"]]
    emit({"status": "ok" if not failed else "failed", "total": len(results), "passed": len(results) - len(failed), "failed": failed, "results": results})
    if args.strict and failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
