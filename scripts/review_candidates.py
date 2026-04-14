#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from classify_memory import LONG_TERM_KINDS, classify
from _common import emit, open_db, read_text, split_frontmatter, store_root


ACTION_PRIORITY = {
    "promote_now": 3,
    "review_after_verification": 2,
    "keep_candidate": 1,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review candidate memories and rank promotion opportunities.")
    parser.add_argument("--store", required=True, help="Path to the external memory-data root")
    parser.add_argument("--subject-id", help="Filter by subject_id")
    parser.add_argument("--subject-name", help="Filter by subject_name")
    parser.add_argument("--top-k", type=int, default=20, help="Maximum candidates to return")
    parser.add_argument(
        "--action",
        choices=["all", "promote_now", "review_after_verification", "keep_candidate"],
        default="all",
        help="Filter by review action",
    )
    parser.add_argument("--min-hits", type=int, default=0, help="Minimum retrieval hits")
    parser.add_argument("--min-age-days", type=float, default=0.0, help="Minimum candidate age in days")
    parser.add_argument("--out-file", help="Write review JSON to a UTF-8 file")
    return parser.parse_args()


def candidate_body(path: Path) -> tuple[str, str]:
    meta, body = split_frontmatter(read_text(path))
    title = path.stem
    for line in body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    return title, body.strip()


def suggested_target(classification: dict[str, object]) -> str:
    kind = str(classification["recommended_kind"])
    if kind in LONG_TERM_KINDS:
        return kind
    return str(classification["underlying_long_term_kind"])


def review_action(classification: dict[str, object], db_confidence: float, hit_count: int, age_days: float) -> str:
    kind = str(classification["recommended_kind"])
    confidence = float(classification["classification_confidence"])
    if kind in LONG_TERM_KINDS and (confidence >= 0.7 or hit_count >= 1 or age_days >= 2):
        return "promote_now"
    if kind in LONG_TERM_KINDS:
        return "review_after_verification"
    if kind in {"candidate", "session"} and (
        hit_count >= 1 or age_days >= 3 or db_confidence >= 0.5
    ):
        return "review_after_verification"
    return "keep_candidate"


def promotion_score(classification: dict[str, object], db_confidence: float, hit_count: int, age_days: float) -> float:
    kind = str(classification["recommended_kind"])
    class_conf = float(classification["classification_confidence"])
    score = db_confidence + class_conf + min(math.log1p(hit_count), 1.5) + min(age_days / 7.0, 1.0)
    if kind in LONG_TERM_KINDS:
        score += 1.2
    elif str(classification["underlying_long_term_kind"]) in LONG_TERM_KINDS:
        score += 0.4
    if kind == "candidate":
        score -= 0.5
    if kind == "session":
        score -= 0.2
    return round(score, 4)


def main() -> None:
    args = parse_args()
    root = store_root(args.store)
    conn = open_db(root)

    clauses = ["LOWER(d.memory_kind) = 'candidate'"]
    params: list[object] = []
    if args.subject_id:
        clauses.append("d.subject_id = ?")
        params.append(args.subject_id)
    if args.subject_name:
        clauses.append("LOWER(d.subject_name) = ?")
        params.append(args.subject_name.casefold())
    if args.min_hits > 0:
        clauses.append("COALESCE(s.hit_count, 0) >= ?")
        params.append(args.min_hits)

    rows = conn.execute(
        f"""
        SELECT
            d.path,
            d.subject_id,
            d.subject_name,
            d.domain,
            d.topic,
            d.summary,
            d.confidence,
            d.status,
            d.mtime,
            COALESCE(s.hit_count, 0) AS hit_count,
            COALESCE(s.rank_score, 0.0) AS rank_score,
            COALESCE(s.last_hit_at, '') AS last_hit_at
        FROM documents AS d
        LEFT JOIN scores AS s ON s.path = d.path
        WHERE {' AND '.join(clauses)}
        """,
        tuple(params),
    ).fetchall()
    conn.close()

    now = datetime.now(timezone.utc)
    reviewed: list[dict[str, object]] = []
    skipped_missing: list[str] = []

    for raw in rows:
        path = Path(str(raw[0]))
        if not path.exists():
            skipped_missing.append(str(path))
            continue

        title, body = candidate_body(path)
        age_days = max(0.0, (now - datetime.fromtimestamp(float(raw[8]), tz=timezone.utc)).total_seconds() / 86400.0)
        if age_days < args.min_age_days:
            continue

        classification = classify(title, body, str(raw[1]), str(raw[2]))
        action = review_action(classification, float(raw[6] or 0.0), int(raw[9] or 0), age_days)
        if args.action != "all" and action != args.action:
            continue

        target_kind = suggested_target(classification)
        score = promotion_score(classification, float(raw[6] or 0.0), int(raw[9] or 0), age_days)
        reviewed.append(
            {
                "path": str(path),
                "title": title,
                "action": action,
                "suggested_target_kind": target_kind,
                "current_confidence": round(float(raw[6] or 0.0), 2),
                "classification_confidence": classification["classification_confidence"],
                "hit_count": int(raw[9] or 0),
                "age_days": round(age_days, 2),
                "promotion_score": score,
                "domain": classification["recommended_domain"],
                "topic": classification["suggested_payload"]["topic"],
                "suggested_tags": classification["suggested_tags"],
                "summary": str(raw[5] or ""),
                "reasons": classification["reasons"],
            }
        )

    reviewed.sort(
        key=lambda item: (ACTION_PRIORITY[item["action"]], item["promotion_score"], item["hit_count"]),
        reverse=True,
    )
    selected = reviewed[: args.top_k]

    result = {
        "status": "ok",
        "returned": len(selected),
        "counts": {
            "promote_now": sum(1 for item in reviewed if item["action"] == "promote_now"),
            "review_after_verification": sum(1 for item in reviewed if item["action"] == "review_after_verification"),
            "keep_candidate": sum(1 for item in reviewed if item["action"] == "keep_candidate"),
        },
        "skipped_missing_files": skipped_missing,
        "candidates": selected,
    }

    if args.out_file:
        Path(args.out_file).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(result)


if __name__ == "__main__":
    main()
