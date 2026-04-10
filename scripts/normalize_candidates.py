#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from _common import emit, parse_args, parse_frontmatter, store_root


def normalize_candidate(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        meta = parse_frontmatter(path)
        changed = False
        if "status" not in meta:
            text = text.replace("---\n", "---\nstatus: pending\n", 1)
            changed = True
        if "scope" not in meta:
            text = text.replace("---\n", "---\nscope: candidate\n", 1)
            changed = True
        if changed:
            path.write_text(text, encoding="utf-8")
        return changed
    path.write_text(
        "---\nscope: candidate\nstatus: pending\n---\n\n# Candidate\n\n" + text,
        encoding="utf-8",
    )
    return True


def main() -> None:
    args = parse_args("Normalize candidate markdown notes.")
    root = store_root(args.store)
    changed = 0
    for path in sorted((root / "candidates").rglob("*.md")):
        if normalize_candidate(path):
            changed += 1
    emit({"status": "ok", "normalized": changed, "store": str(root)})


if __name__ == "__main__":
    main()
