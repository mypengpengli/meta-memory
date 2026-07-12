#!/usr/bin/env python3
"""Ingest local reference material as raw evidence without promoting it to user facts."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
from pathlib import Path

from _common import DEFAULT_STORE_HELP, compose_markdown, emit, ensure_default_dirs, sha256_text, store_root
from ingest_raw_event import insert_raw_event


SUPPORTED = {".md", ".txt", ".json", ".jsonl", ".csv", ".yaml", ".yml", ".html", ".htm"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preserve a local resource as raw, auditable memory evidence.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--subject-id", required=True)
    parser.add_argument("--subject-name", default="Unknown")
    parser.add_argument("--file", required=True)
    parser.add_argument("--session-id", default="")
    parser.add_argument("--topic-hint", default="")
    parser.add_argument("--domain-hint", default="")
    parser.add_argument("--max-chars", type=int, default=20000)
    return parser.parse_args()


def resource_text(path: Path, limit: int) -> str:
    suffix = path.suffix.casefold()
    if suffix not in SUPPORTED:
        raise ValueError(f"Unsupported resource type: {suffix}. Supported: {', '.join(sorted(SUPPORTED))}")
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if suffix in {".html", ".htm"}:
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    elif suffix == ".csv":
        rows = list(csv.reader(text.splitlines()))
        text = "\n".join(" | ".join(row) for row in rows)
    elif suffix in {".json", ".jsonl"}:
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2) if suffix == ".json" else text
        except json.JSONDecodeError:
            pass
    return text[: max(1, limit)]


def main() -> None:
    args = parse_args()
    root = store_root(args.store)
    ensure_default_dirs(root)
    source = Path(args.file).expanduser().resolve()
    if not source.is_file():
        raise SystemExit("--file must name a readable file")
    content = resource_text(source, args.max_chars)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    card = root / "resources" / f"{digest[:16]}.md"
    card.write_text(
        compose_markdown(
            {"schema_version": 2, "resource_hash": digest, "source_path": str(source), "source_type": source.suffix.casefold().lstrip("."), "subject_id": args.subject_id, "topic": args.topic_hint, "domain": args.domain_hint},
            f"# {source.name}\n\n## Resource Summary\n\n- SHA-256: `{digest}`\n- Imported characters: {len(content)}\n- This card is source evidence, not a user fact.\n",
        ),
        encoding="utf-8",
    )
    event = insert_raw_event(root, subject_id=args.subject_id, subject_name=args.subject_name, session_id=args.session_id, source_type="resource", source_ref=str(source), topic_hint=args.topic_hint, domain_hint=args.domain_hint, content=content)
    emit({"status": "ok", "resource_card": str(card), "resource_hash": digest, "raw_event": event, "truncated": len(content) >= args.max_chars})


if __name__ == "__main__":
    main()
