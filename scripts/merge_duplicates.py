#!/usr/bin/env python3
from __future__ import annotations

from collections import defaultdict

from _common import emit, first_heading, markdown_files, parse_args, store_root


def main() -> None:
    args = parse_args("Report duplicate memory note titles.")
    root = store_root(args.store)
    buckets: dict[str, list[str]] = defaultdict(list)
    for path in markdown_files(
        [root / "fixed", root / "topics", root / "projects", root / "candidates"]
    ):
        buckets[first_heading(path).strip().lower()].append(str(path))
    duplicates = {title: paths for title, paths in buckets.items() if len(paths) > 1}
    emit({"status": "ok", "duplicates": duplicates, "count": len(duplicates)})


if __name__ == "__main__":
    main()
