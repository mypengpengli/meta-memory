#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from classify_memory import LONG_TERM_KINDS, classify
from write_memory import DEFAULT_CONFIDENCE, DEFAULT_STATUS, as_list, slugify, write_payload
from _common import DEFAULT_STORE_HELP, emit, store_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify a memory and write it into the appropriate layer in one step."
    )
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--title", help="Memory title")
    parser.add_argument("--title-file", help="Read title from a UTF-8 text file")
    parser.add_argument("--content", help="Inline content")
    parser.add_argument("--content-file", help="Read content from a UTF-8 text file")
    parser.add_argument("--payload-file", help="Read title/content/metadata from a UTF-8 JSON file")
    parser.add_argument("--subject-id", default="person-unknown", help="Primary subject id")
    parser.add_argument("--subject-name", default="Unknown", help="Primary subject display name")
    parser.add_argument(
        "--force-kind",
        choices=["profile", "state", "event", "relationship", "goal", "domain", "session", "candidate"],
        help="Override the classifier result",
    )
    parser.add_argument(
        "--use-underlying-kind",
        action="store_true",
        help="If the classifier recommends session/candidate, write to the suggested long-term kind instead",
    )
    parser.add_argument("--domain", help="Override domain")
    parser.add_argument("--topic", help="Override topic")
    parser.add_argument("--source", help="Override source")
    parser.add_argument("--start-at", help="Override start time")
    parser.add_argument("--end-at", help="Override end time")
    parser.add_argument("--confidence", type=float, help="Override confidence")
    parser.add_argument("--importance", type=float, help="Override importance score from 0.0 to 1.0")
    parser.add_argument("--status", help="Override status")
    parser.add_argument("--tag", action="append", default=[], help="Additional tag; may be repeated")
    parser.add_argument("--related-person", action="append", default=[], help="Related person; may be repeated")
    parser.add_argument("--related-event", action="append", default=[], help="Related event; may be repeated")
    parser.add_argument("--related-topic", action="append", default=[], help="Related topic/entity; may be repeated")
    parser.add_argument("--related-source", action="append", default=[], help="Related source; may be repeated")
    parser.add_argument("--slug", help="Override slug")
    parser.add_argument(
        "--mode",
        choices=["create", "replace", "append"],
        default="create",
        help="How to behave if the target file already exists",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only print the classified payload, do not write")
    parser.add_argument("--skip-index", action="store_true", help="Do not reindex/rescore after writing")
    parser.add_argument("--out-file", help="Write the result JSON to a UTF-8 file")
    return parser.parse_args()


def load_payload(path: str | None) -> dict[str, object]:
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def read_input(args: argparse.Namespace, payload: dict[str, object]) -> tuple[str, str]:
    if args.title_file:
        title = Path(args.title_file).read_text(encoding="utf-8-sig").strip()
    else:
        title = args.title or str(payload.get("title", "")).strip()
    if args.content_file:
        content = Path(args.content_file).read_text(encoding="utf-8-sig").strip()
    elif args.content:
        content = args.content.strip()
    else:
        content = str(payload.get("content", "")).strip()
    if not title:
        title = content.splitlines()[0][:40].strip() or "Untitled Memory"
    if not content:
        raise SystemExit("Content is required via --content, --content-file, or --payload-file.")
    return title, content


def merge_unique(base: list[str], extra: list[str]) -> list[str]:
    merged = list(base)
    for item in extra:
        if item and item not in merged:
            merged.append(item)
    return merged


def build_payload(
    classification: dict[str, object],
    payload: dict[str, object],
    args: argparse.Namespace,
    title: str,
    content: str,
) -> dict[str, object]:
    suggested = dict(classification["suggested_payload"])
    final_kind = classification["recommended_kind"]
    if args.use_underlying_kind and final_kind in {"candidate", "session"}:
        final_kind = str(classification["underlying_long_term_kind"])
    if args.force_kind:
        final_kind = args.force_kind

    final_payload: dict[str, object] = dict(suggested)
    final_payload["title"] = title
    final_payload["content"] = content
    final_payload["kind"] = final_kind
    final_payload["subject_id"] = payload.get("subject_id", args.subject_id)
    final_payload["subject_name"] = payload.get("subject_name", args.subject_name)
    final_payload["mode"] = payload.get("mode", args.mode)
    final_payload["slug"] = payload.get("slug", args.slug or slugify(title))

    for key, arg_value in [
        ("domain", args.domain),
        ("topic", args.topic),
        ("source", args.source),
        ("start_at", args.start_at),
        ("end_at", args.end_at),
        ("status", args.status),
    ]:
        if arg_value not in (None, ""):
            final_payload[key] = arg_value
        elif key in payload and payload[key] not in (None, ""):
            final_payload[key] = payload[key]

    if args.confidence is not None:
        final_payload["confidence"] = args.confidence
    elif "confidence" in payload and payload["confidence"] not in (None, ""):
        final_payload["confidence"] = payload["confidence"]

    if args.importance is not None:
        final_payload["importance"] = args.importance
    elif "importance" in payload and payload["importance"] not in (None, ""):
        final_payload["importance"] = payload["importance"]

    final_payload["tags"] = merge_unique(
        as_list(payload.get("tags", suggested.get("tags", []))),
        args.tag,
    )
    final_payload["related_people"] = merge_unique(
        as_list(payload.get("related_people", [])),
        args.related_person,
    )
    final_payload["related_events"] = merge_unique(
        as_list(payload.get("related_events", [])),
        args.related_event,
    )
    final_payload["related_topics"] = merge_unique(
        as_list(payload.get("related_topics", [])),
        args.related_topic,
    )
    final_payload["related_sources"] = merge_unique(
        as_list(payload.get("related_sources", [])),
        args.related_source,
    )
    final_payload["supersedes"] = as_list(payload.get("supersedes", []))
    final_payload["replaced_by"] = as_list(payload.get("replaced_by", []))

    if final_kind != classification["recommended_kind"] and not args.status and "status" not in payload:
        final_payload["status"] = DEFAULT_STATUS[final_kind]
    if (
        final_kind in LONG_TERM_KINDS
        and classification["recommended_kind"] in {"candidate", "session"}
        and args.confidence is None
        and "confidence" not in payload
    ):
        final_payload["confidence"] = max(
            float(final_payload.get("confidence", 0.0) or 0.0),
            DEFAULT_CONFIDENCE[final_kind],
        )

    return final_payload


def main() -> None:
    args = parse_args()
    payload = load_payload(args.payload_file)
    title, content = read_input(args, payload)
    subject_id = str(payload.get("subject_id", args.subject_id))
    subject_name = str(payload.get("subject_name", args.subject_name))

    classification = classify(title, content, subject_id, subject_name)
    final_payload = build_payload(classification, payload, args, title, content)

    result: dict[str, object] = {
        "status": "ok",
        "classification": classification,
        "final_payload": final_payload,
        "written": None,
    }

    if not args.dry_run:
        root = store_root(args.store)
        result["written"] = write_payload(root, final_payload, skip_index=args.skip_index)

    if args.out_file:
        Path(args.out_file).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(result)


if __name__ == "__main__":
    main()
