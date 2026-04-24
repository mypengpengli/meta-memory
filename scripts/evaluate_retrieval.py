#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, store_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate retrieval cases against expected memory matches.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--cases-file", required=True, help="UTF-8 JSON file containing a list of retrieval cases")
    parser.add_argument("--top-k", type=int, default=6)
    parser.add_argument("--candidate-pool", type=int, default=32)
    parser.add_argument("--expand-hops", type=int, default=1)
    parser.add_argument("--strict", action="store_true", help="Exit with code 1 when any case fails")
    return parser.parse_args()


def load_cases(path: str) -> list[dict[str, object]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        payload = payload.get("cases", [])
    if not isinstance(payload, list):
        raise SystemExit("cases-file must contain a JSON list or an object with a `cases` list.")
    return [case for case in payload if isinstance(case, dict)]


def as_list(value: object) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if str(value).strip() else []


def searchable_text(item: dict[str, object]) -> str:
    return "\n".join(
        [
            str(item.get("path", "")),
            str(item.get("title", "")),
            str(item.get("memory_kind", "")),
            str(item.get("domain", "")),
            str(item.get("topic", "")),
            str(item.get("summary", "")),
        ]
    ).casefold()


def run_retrieve(root: Path, case: dict[str, object], args: argparse.Namespace) -> dict[str, object]:
    query = str(case.get("query", "")).strip()
    if not query:
        raise ValueError("case.query is required")

    command = [
        sys.executable,
        str(Path(__file__).resolve().parent / "retrieve_memories.py"),
        "--store",
        str(root),
        "--query",
        query,
        "--top-k",
        str(int(case.get("top_k", args.top_k))),
        "--candidate-pool",
        str(int(case.get("candidate_pool", args.candidate_pool))),
        "--expand-hops",
        str(int(case.get("expand_hops", args.expand_hops))),
    ]
    for key, option in [("subject_id", "--subject-id"), ("subject_name", "--subject-name")]:
        value = str(case.get(key, "")).strip()
        if value:
            command.extend([option, value])
    for domain in as_list(case.get("domains", case.get("domain", []))):
        command.extend(["--domain", domain])
    for kind in as_list(case.get("memory_kinds", case.get("memory_kind", []))):
        command.extend(["--memory-kind", kind])
    if bool(case.get("include_candidates", False)):
        command.append("--include-candidates")

    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def evaluate_case(root: Path, case: dict[str, object], args: argparse.Namespace) -> dict[str, object]:
    retrieved = run_retrieve(root, case, args)
    selected = list(retrieved.get("selected", []))
    haystacks = [searchable_text(item) for item in selected if isinstance(item, dict)]

    expected = as_list(case.get("must_include", case.get("expected", [])))
    forbidden = as_list(case.get("must_not_include", case.get("forbidden", [])))
    missing = [needle for needle in expected if not any(needle.casefold() in haystack for haystack in haystacks)]
    unexpected = [needle for needle in forbidden if any(needle.casefold() in haystack for haystack in haystacks)]
    passed = not missing and not unexpected

    return {
        "name": str(case.get("name", case.get("query", ""))),
        "passed": passed,
        "query": retrieved.get("query", ""),
        "returned": retrieved.get("returned", 0),
        "selected_titles": [str(item.get("title", "")) for item in selected if isinstance(item, dict)],
        "missing": missing,
        "unexpected": unexpected,
    }


def main() -> None:
    args = parse_args()
    root = store_root(args.store)
    cases = load_cases(args.cases_file)
    results = [evaluate_case(root, case, args) for case in cases]
    passed = sum(1 for result in results if result["passed"])
    total = len(results)
    payload = {
        "status": "ok" if passed == total else "fail",
        "store": str(root),
        "case_count": total,
        "passed": passed,
        "failed": total - passed,
        "recall_at_k": round(passed / total, 4) if total else 0.0,
        "results": results,
    }
    emit(payload)
    if args.strict and passed != total:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
