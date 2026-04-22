#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from _common import emit, parse_args, store_root


def run(script: Path, store: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, str(script), "--store", store],
        check=True,
        capture_output=True,
        text=True,
    )
    return {"script": script.name, "stdout": result.stdout.strip()}


def main() -> None:
    args = parse_args("Run the standard memory maintenance sequence.")
    root = store_root(args.store)
    base = Path(__file__).resolve().parent
    scripts = [
        base / "normalize_candidates.py",
        base / "reindex_memory.py",
        base / "merge_duplicates.py",
        base / "score_memories.py",
        base / "build_views.py",
        base / "lint_memory.py",
    ]
    results = [run(script, str(root)) for script in scripts]
    emit({"status": "ok", "steps": results})


if __name__ == "__main__":
    main()
