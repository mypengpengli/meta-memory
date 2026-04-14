#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from _common import compose_markdown, emit, parse_args, read_text, split_frontmatter, store_root


DEFAULT_META = {
    "subject_id": "person-unknown",
    "subject_name": "Unknown",
    "memory_kind": "candidate",
    "domain": "general",
    "topic": "candidate",
    "tags": [],
    "start_at": "",
    "end_at": "",
    "confidence": 0.3,
    "status": "pending",
    "source": "",
    "related_people": [],
    "related_events": [],
    "related_sources": [],
    "supersedes": [],
    "replaced_by": [],
}


def has_heading(body: str) -> bool:
    return any(line.startswith("# ") for line in body.splitlines())


def normalize_candidate(path: Path) -> bool:
    meta, body = split_frontmatter(read_text(path))
    changed = False

    for key, value in DEFAULT_META.items():
        if key not in meta:
            meta[key] = value if not isinstance(value, list) else list(value)
            changed = True

    if meta.get("topic") in ("", "candidate"):
        meta["topic"] = path.stem
        changed = True

    if not body.strip():
        body = f"# {path.stem}\n\n待整理。"
        changed = True
    elif not has_heading(body):
        body = f"# {path.stem}\n\n{body.lstrip()}"
        changed = True

    if changed:
        path.write_text(compose_markdown(meta, body), encoding="utf-8")
    return changed


def main() -> None:
    args = parse_args("Normalize candidate markdown notes for the person-centered schema.")
    root = store_root(args.store)
    changed = 0
    for path in sorted((root / "candidates").rglob("*.md")):
        if normalize_candidate(path):
            changed += 1
    emit({"status": "ok", "normalized": changed, "store": str(root)})


if __name__ == "__main__":
    main()
