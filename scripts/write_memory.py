#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from _common import compose_markdown, emit, ensure_default_dirs, parse_frontmatter, read_text, split_frontmatter, store_root


KIND_DIRS = {
    "profile": "profile",
    "state": "states",
    "event": "events",
    "relationship": "relationships",
    "goal": "goals",
    "domain": "domains",
    "session": "sessions",
    "candidate": "candidates",
    "archive": "archive/raw",
}

DEFAULT_CONFIDENCE = {
    "profile": 0.9,
    "state": 0.7,
    "event": 0.8,
    "relationship": 0.75,
    "goal": 0.75,
    "domain": 0.8,
    "session": 0.5,
    "candidate": 0.3,
    "archive": 1.0,
}

DEFAULT_STATUS = {
    "profile": "active",
    "state": "active",
    "event": "historical",
    "relationship": "active",
    "goal": "active",
    "domain": "active",
    "session": "active",
    "candidate": "pending",
    "archive": "historical",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write a structured memory note into the external store.")
    parser.add_argument("--store", required=True, help="Path to the external memory-data root")
    parser.add_argument("--title", help="Memory note title")
    parser.add_argument(
        "--kind",
        choices=sorted(KIND_DIRS),
        default=None,
        help="Target memory kind",
    )
    parser.add_argument("--payload-file", help="Read title/content/metadata from a UTF-8 JSON file")
    parser.add_argument("--content", help="Inline content for the memory note")
    parser.add_argument("--content-file", help="Read note content from a file")
    parser.add_argument("--subject-id", help="Primary subject id")
    parser.add_argument("--subject-name", help="Primary subject display name")
    parser.add_argument("--domain", help="Related domain")
    parser.add_argument("--topic", help="Primary topic")
    parser.add_argument("--source", help="Source or evidence reference")
    parser.add_argument("--start-at", help="When this memory started to apply")
    parser.add_argument("--end-at", help="When this memory stopped applying")
    parser.add_argument("--confidence", type=float, help="Confidence score")
    parser.add_argument("--status", help="Memory status")
    parser.add_argument("--tag", action="append", default=[], help="Tag; may be repeated")
    parser.add_argument(
        "--related-person",
        action="append",
        default=[],
        help="Related person; may be repeated",
    )
    parser.add_argument(
        "--related-event",
        action="append",
        default=[],
        help="Related event; may be repeated",
    )
    parser.add_argument(
        "--related-source",
        action="append",
        default=[],
        help="Related source; may be repeated",
    )
    parser.add_argument("--slug", help="Filename slug without extension")
    parser.add_argument(
        "--mode",
        choices=["create", "replace", "append"],
        default=None,
        help="How to behave if the target file already exists",
    )
    parser.add_argument(
        "--skip-index",
        action="store_true",
        help="Do not reindex and rescore after writing",
    )
    return parser.parse_args()


def slugify(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." or ("\u4e00" <= ch <= "\u9fff") else "-" for ch in text.strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-_.") or "note"


def read_content(args: argparse.Namespace) -> str:
    if args.content_file:
        return Path(args.content_file).read_text(encoding="utf-8-sig").strip()
    if args.content:
        return args.content.strip()
    return ""


def load_payload(path: str | None) -> dict[str, object]:
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def arg_or_payload(args: argparse.Namespace, payload: dict[str, object], attr: str, default: object = "") -> object:
    value = getattr(args, attr)
    if value not in (None, "", []):
        return value
    return payload.get(attr, default)


def preferred_filename(kind: str, slug: str) -> str:
    if kind in {"session", "candidate", "event", "archive"}:
        return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slug}.md"
    return f"{slug}.md"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def resolve_path(root: Path, kind: str, slug: str, mode: str) -> Path:
    folder = root / KIND_DIRS[kind]
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / preferred_filename(kind, slug)
    if mode == "create":
        return unique_path(target)
    return target


def as_list(value: object) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if str(value).strip() else []


def merge_unique(existing: object, new_items: list[str]) -> list[object]:
    merged = as_list(existing)
    for item in new_items:
        if item.strip() and item not in merged:
            merged.append(item)
    return merged


def build_meta_from_payload(payload: dict[str, object], title: str) -> dict[str, object]:
    kind = str(payload.get("kind", "candidate"))
    return {
        "subject_id": str(payload.get("subject_id", "person-unknown")),
        "subject_name": str(payload.get("subject_name", "Unknown")),
        "memory_kind": kind,
        "domain": str(payload.get("domain", "general")),
        "topic": str(payload.get("topic", slugify(title))),
        "tags": as_list(payload.get("tags", payload.get("tag", []))),
        "start_at": str(payload.get("start_at", "")),
        "end_at": str(payload.get("end_at", "")),
        "confidence": float(payload.get("confidence", DEFAULT_CONFIDENCE[kind])),
        "status": str(payload.get("status", DEFAULT_STATUS[kind])),
        "source": str(payload.get("source", "")),
        "related_people": as_list(payload.get("related_people", payload.get("related_person", []))),
        "related_events": as_list(payload.get("related_events", payload.get("related_event", []))),
        "related_sources": as_list(payload.get("related_sources", payload.get("related_source", []))),
        "supersedes": as_list(payload.get("supersedes", [])),
        "replaced_by": as_list(payload.get("replaced_by", [])),
    }


def build_meta(args: argparse.Namespace, payload: dict[str, object], title: str) -> dict[str, object]:
    kind = str(arg_or_payload(args, payload, "kind", "candidate"))
    merged_payload = {
        "kind": kind,
        "subject_id": str(arg_or_payload(args, payload, "subject_id", "person-unknown")),
        "subject_name": str(arg_or_payload(args, payload, "subject_name", "Unknown")),
        "domain": str(arg_or_payload(args, payload, "domain", "general")),
        "topic": str(arg_or_payload(args, payload, "topic", slugify(title))),
        "tags": as_list(arg_or_payload(args, payload, "tag", payload.get("tags", []))),
        "start_at": str(arg_or_payload(args, payload, "start_at", "")),
        "end_at": str(arg_or_payload(args, payload, "end_at", "")),
        "confidence": float(
            arg_or_payload(
                args,
                payload,
                "confidence",
                DEFAULT_CONFIDENCE[kind],
            )
        ),
        "status": str(arg_or_payload(args, payload, "status", DEFAULT_STATUS[kind])),
        "source": str(arg_or_payload(args, payload, "source", "")),
        "related_people": as_list(
            arg_or_payload(args, payload, "related_person", payload.get("related_people", []))
        ),
        "related_events": as_list(
            arg_or_payload(args, payload, "related_event", payload.get("related_events", []))
        ),
        "related_sources": as_list(
            arg_or_payload(args, payload, "related_source", payload.get("related_sources", []))
        ),
        "supersedes": as_list(payload.get("supersedes", [])),
        "replaced_by": as_list(payload.get("replaced_by", [])),
    }
    return build_meta_from_payload(merged_payload, title)


def build_body(title: str, content: str) -> str:
    return f"# {title}\n\n{content.strip()}"


def append_body(existing_body: str, content: str) -> str:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    section = f"## Update {timestamp}\n\n{content.strip()}"
    if existing_body.strip():
        return f"{existing_body.rstrip()}\n\n{section}"
    return section


def run_indexing(store: Path) -> list[dict[str, object]]:
    base = Path(__file__).resolve().parent
    steps: list[dict[str, object]] = []
    for script_name in ("reindex_memory.py", "score_memories.py"):
        result = subprocess.run(
            [sys.executable, str(base / script_name), "--store", str(store)],
            check=True,
            capture_output=True,
            text=True,
        )
        steps.append({"script": script_name, "stdout": result.stdout.strip()})
    return steps


def write_payload(root: Path, payload: dict[str, object], skip_index: bool = False) -> dict[str, object]:
    ensure_default_dirs(root)

    title = str(payload.get("title", "")).strip()
    if not title:
        raise ValueError("payload.title is required")

    content = str(payload.get("content", "")).strip()
    if not content:
        raise ValueError("payload.content is required")

    kind = str(payload.get("kind", "candidate") or "candidate")
    if kind not in KIND_DIRS:
        raise ValueError(f"Unsupported memory kind: {kind}")

    mode = str(payload.get("mode", "create") or "create")
    if mode not in {"create", "replace", "append"}:
        raise ValueError(f"Unsupported write mode: {mode}")

    slug = slugify(str(payload.get("slug", title)))
    target = resolve_path(root, kind, slug, mode)
    meta = build_meta_from_payload(payload, title)

    if mode == "append" and target.exists():
        existing_meta, existing_body = split_frontmatter(read_text(target))
        if not existing_meta:
            existing_meta = parse_frontmatter(target)
        merged = dict(existing_meta)
        for key, value in meta.items():
            if isinstance(value, list):
                merged[key] = merge_unique(merged.get(key, []), value)
            elif value not in ("", None):
                merged[key] = value
            elif key not in merged:
                merged[key] = value
        body = append_body(existing_body, content)
        target.write_text(compose_markdown(merged, body), encoding="utf-8")
    else:
        body = build_body(title, content)
        target.write_text(compose_markdown(meta, body), encoding="utf-8")

    steps: list[dict[str, object]] = []
    if not skip_index:
        steps = run_indexing(root)

    return {
        "status": "ok",
        "path": str(target),
        "kind": kind,
        "mode": mode,
        "indexed": not skip_index,
        "steps": steps,
    }


def main() -> None:
    args = parse_args()
    payload = load_payload(args.payload_file)
    title = str(arg_or_payload(args, payload, "title", "")).strip()
    if not title:
        raise SystemExit("A title is required via --title or --payload-file.")

    content = read_content(args)
    if not content:
        content = str(payload.get("content", "")).strip()
    if not content:
        raise SystemExit("Content is required via --content, --content-file, or --payload-file.")

    root = store_root(args.store)
    kind = str(arg_or_payload(args, payload, "kind", "candidate"))
    mode = str(arg_or_payload(args, payload, "mode", "create") or "create")
    slug = str(arg_or_payload(args, payload, "slug", args.slug or title))

    final_payload = {
        "title": title,
        "content": content,
        "kind": kind,
        "mode": mode,
        "slug": slug,
        "subject_id": str(arg_or_payload(args, payload, "subject_id", "person-unknown")),
        "subject_name": str(arg_or_payload(args, payload, "subject_name", "Unknown")),
        "domain": str(arg_or_payload(args, payload, "domain", "general")),
        "topic": str(arg_or_payload(args, payload, "topic", slugify(title))),
        "tags": as_list(arg_or_payload(args, payload, "tag", payload.get("tags", []))),
        "start_at": str(arg_or_payload(args, payload, "start_at", "")),
        "end_at": str(arg_or_payload(args, payload, "end_at", "")),
        "confidence": float(
            arg_or_payload(
                args,
                payload,
                "confidence",
                DEFAULT_CONFIDENCE[kind],
            )
        ),
        "status": str(arg_or_payload(args, payload, "status", DEFAULT_STATUS[kind])),
        "source": str(arg_or_payload(args, payload, "source", "")),
        "related_people": as_list(
            arg_or_payload(args, payload, "related_person", payload.get("related_people", []))
        ),
        "related_events": as_list(
            arg_or_payload(args, payload, "related_event", payload.get("related_events", []))
        ),
        "related_sources": as_list(
            arg_or_payload(args, payload, "related_source", payload.get("related_sources", []))
        ),
        "supersedes": as_list(payload.get("supersedes", [])),
        "replaced_by": as_list(payload.get("replaced_by", [])),
    }

    emit(write_payload(root, final_payload, skip_index=args.skip_index))


if __name__ == "__main__":
    main()
