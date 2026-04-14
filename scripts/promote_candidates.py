#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from _common import DEFAULT_STORE_HELP, compose_markdown, emit, ensure_default_dirs, read_text, split_frontmatter, store_root
from normalize_candidates import DEFAULT_META as CANDIDATE_DEFAULTS
from write_memory import (
    DEFAULT_CONFIDENCE,
    DEFAULT_STATUS,
    as_list,
    append_body,
    merge_unique,
    resolve_path,
    run_indexing,
    slugify,
)


PROMOTABLE_KINDS = ["profile", "state", "event", "relationship", "goal", "domain"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Promote a candidate memory into a long-term memory note."
    )
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument(
        "--candidate",
        required=True,
        help="Candidate markdown path, absolute or relative to the store root",
    )
    parser.add_argument(
        "--target-kind",
        required=True,
        choices=PROMOTABLE_KINDS,
        help="Long-term memory kind to promote into",
    )
    parser.add_argument("--title", help="Override promoted title")
    parser.add_argument("--slug", help="Override promoted slug")
    parser.add_argument("--domain", help="Override promoted domain")
    parser.add_argument("--topic", help="Override promoted topic")
    parser.add_argument("--source", help="Override source field")
    parser.add_argument("--start-at", help="Override start time")
    parser.add_argument("--end-at", help="Override end time")
    parser.add_argument("--confidence", type=float, help="Override confidence")
    parser.add_argument("--status", help="Override status")
    parser.add_argument("--tag", action="append", default=[], help="Additional tag; may be repeated")
    parser.add_argument(
        "--related-person",
        action="append",
        default=[],
        help="Additional related person; may be repeated",
    )
    parser.add_argument(
        "--related-event",
        action="append",
        default=[],
        help="Additional related event; may be repeated",
    )
    parser.add_argument(
        "--mode",
        choices=["create", "replace", "append"],
        default="create",
        help="How to write the promoted target note",
    )
    parser.add_argument(
        "--keep-candidate",
        action="store_true",
        help="Keep the original candidate file in candidates/ after promotion",
    )
    parser.add_argument(
        "--skip-index",
        action="store_true",
        help="Do not reindex and rescore after promotion",
    )
    return parser.parse_args()


def resolve_candidate(root: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (root / raw).resolve()


def load_candidate(path: Path) -> tuple[dict[str, object], str]:
    meta, body = split_frontmatter(read_text(path))
    if not meta:
        meta = {}
    for key, value in CANDIDATE_DEFAULTS.items():
        if key not in meta:
            meta[key] = value if not isinstance(value, list) else list(value)
    return meta, body


def promoted_meta(
    source_paths: list[str],
    candidate_meta: dict[str, object],
    args: argparse.Namespace,
    title: str,
) -> dict[str, object]:
    kind = args.target_kind
    tag_values = merge_unique(candidate_meta.get("tags", []), args.tag)
    related_people = merge_unique(candidate_meta.get("related_people", []), args.related_person)
    related_events = merge_unique(candidate_meta.get("related_events", []), args.related_event)
    related_sources = merge_unique(candidate_meta.get("related_sources", []), source_paths)
    raw_source = (
        args.source
        if args.source is not None
        else str(candidate_meta.get("source", "")).strip()
    )
    if not raw_source:
        raw_source = "promoted-from-candidate"

    return {
        "subject_id": str(candidate_meta.get("subject_id", "person-unknown")),
        "subject_name": str(candidate_meta.get("subject_name", "Unknown")),
        "memory_kind": kind,
        "domain": args.domain or str(candidate_meta.get("domain", "general")),
        "topic": args.topic or str(candidate_meta.get("topic", slugify(title))),
        "tags": tag_values,
        "start_at": args.start_at if args.start_at is not None else str(candidate_meta.get("start_at", "")),
        "end_at": args.end_at if args.end_at is not None else str(candidate_meta.get("end_at", "")),
        "confidence": args.confidence
        if args.confidence is not None
        else max(float(candidate_meta.get("confidence", 0.0) or 0.0), DEFAULT_CONFIDENCE[kind]),
        "status": args.status or DEFAULT_STATUS[kind],
        "source": raw_source,
        "related_people": related_people,
        "related_events": related_events,
        "related_sources": related_sources,
        "supersedes": as_list(candidate_meta.get("supersedes", [])),
        "replaced_by": as_list(candidate_meta.get("replaced_by", [])),
    }


def archive_candidate(
    root: Path,
    candidate_path: Path,
    candidate_meta: dict[str, object],
    candidate_body: str,
    target_path: Path,
    keep_candidate: bool,
) -> str:
    candidate_meta = dict(candidate_meta)
    candidate_meta["status"] = "promoted"
    candidate_meta["replaced_by"] = merge_unique(candidate_meta.get("replaced_by", []), [str(target_path)])
    archived_content = compose_markdown(candidate_meta, candidate_body)

    archive_dir = root / "archive" / "imports" / "promoted"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived_name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{candidate_path.stem}.md"
    archived_path = archive_dir / archived_name
    archived_path.write_text(archived_content, encoding="utf-8")

    if keep_candidate:
        candidate_path.write_text(compose_markdown(candidate_meta, archived_body), encoding="utf-8")
    else:
        candidate_path.unlink()
    return str(archived_path)


def retitle_body(body: str, title: str) -> str:
    lines = body.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith("# "):
            lines[idx] = f"# {title}"
            return "\n".join(lines).strip()
    if body.strip():
        return f"# {title}\n\n{body.lstrip()}".strip()
    return f"# {title}\n\n待整理。"


def main() -> None:
    args = parse_args()
    root = store_root(args.store)
    ensure_default_dirs(root)

    candidate_path = resolve_candidate(root, args.candidate)
    if not candidate_path.exists():
        raise SystemExit(f"Candidate not found: {candidate_path}")

    candidate_meta, candidate_body = load_candidate(candidate_path)
    title = args.title or candidate_path.stem
    slug = slugify(args.slug or title)
    target_path = resolve_path(root, args.target_kind, slug, args.mode)
    archived_path = archive_candidate(root, candidate_path, candidate_meta, candidate_body, target_path, args.keep_candidate)
    source_paths = [archived_path]
    if args.keep_candidate:
        source_paths.append(str(candidate_path))
    meta = promoted_meta(source_paths, candidate_meta, args, title)

    if args.mode == "append" and target_path.exists():
        target_meta, target_body = split_frontmatter(read_text(target_path))
        merged = dict(target_meta)
        for key, value in meta.items():
            if isinstance(value, list):
                merged[key] = merge_unique(merged.get(key, []), value)
            elif value not in ("", None):
                merged[key] = value
            elif key not in merged:
                merged[key] = value
        body = append_body(target_body, retitle_body(candidate_body, title))
        target_path.write_text(compose_markdown(merged, body), encoding="utf-8")
    else:
        body = retitle_body(candidate_body, title)
        target_path.write_text(compose_markdown(meta, body), encoding="utf-8")

    steps: list[dict[str, object]] = []
    if not args.skip_index:
        steps = run_indexing(root)

    emit(
        {
            "status": "ok",
            "candidate": str(candidate_path),
            "promoted_to": str(target_path),
            "archived_candidate": archived_path,
            "kind": args.target_kind,
            "mode": args.mode,
            "indexed": not args.skip_index,
            "steps": steps,
        }
    )


if __name__ == "__main__":
    main()
