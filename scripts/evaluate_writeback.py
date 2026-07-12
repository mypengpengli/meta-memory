#!/usr/bin/env python3
"""Evaluate memory-plan validation rules from a JSON case file."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, store_root
from validate_memory_plan import validate_plan


def main() -> None:
    parser = argparse.ArgumentParser(description="Run writeback-validation evaluation cases.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--cases-file", required=True)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    cases = json.loads(Path(args.cases_file).read_text(encoding="utf-8-sig"))
    if not isinstance(cases, list):
        raise SystemExit("Cases file must be a JSON array")
    root = store_root(args.store)
    results: list[dict[str, object]] = []
    for case in cases:
        report = validate_plan(root, dict(case["plan"]))
        expected = bool(case.get("valid", True))
        passed = bool(report["valid"]) == expected
        results.append({"name": case.get("name", "unnamed"), "passed": passed, "expected_valid": expected, "actual_valid": report["valid"], "errors": report["errors"]})
    failed = [item for item in results if not item["passed"]]
    payload = {"status": "ok" if not failed else "failed", "total": len(results), "passed": len(results) - len(failed), "failed": failed, "results": results}
    emit(payload)
    if args.strict and failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
