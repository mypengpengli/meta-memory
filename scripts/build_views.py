#!/usr/bin/env python3
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root


KIND_LABELS = {
    "profile": "画像",
    "state": "状态",
    "goal": "目标",
    "relationship": "关系",
    "event": "时间线",
    "domain": "领域",
    "session": "会话",
    "candidate": "候选池",
}


def parse_args():
    import argparse

    parser = argparse.ArgumentParser(description="Build human-readable markdown views for the memory store.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--recent-events", type=int, default=80, help="How many raw events to render in log.md")
    return parser.parse_args()


def rel_link(root: Path, raw_path: str) -> str:
    try:
        relative = Path(raw_path).resolve().relative_to(root).as_posix()
    except ValueError:
        relative = Path(raw_path).name
    return relative


def render_index(root: Path, docs: list[dict[str, object]]) -> str:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in docs:
        subject_id = str(row["subject_id"] or "subject-unknown")
        grouped[subject_id].append(row)

    lines = [
        "# Memory Index",
        "",
        "这份索引只列出已经整理过的记忆页。",
        "原始来源和对话事件仍然保存在 `raw_events` / `log.md`，不会和正式记忆混在一起。",
        "",
    ]

    if not grouped:
        lines.append("当前还没有正式记忆页。")
        return "\n".join(lines).strip() + "\n"

    for subject_id in sorted(grouped):
        rows = grouped[subject_id]
        subject_name = next((str(item["subject_name"] or "").strip() for item in rows if str(item["subject_name"] or "").strip()), subject_id)
        lines.extend([f"## {subject_name} (`{subject_id}`)", ""])

        canonical = [row for row in rows if int(row["canonical"] or 0) == 1]
        if canonical:
            lines.append("### Canonical Pages")
            for row in sorted(canonical, key=lambda item: (str(item["memory_kind"]), str(item["title"]))):
                label = KIND_LABELS.get(str(row["memory_kind"]), str(row["memory_kind"]))
                lines.append(
                    f"- `{label}`: [{row['title']}]({rel_link(root, str(row['path']))})"
                )
            lines.append("")

        counter = Counter(str(row["memory_kind"]) for row in rows)
        if counter:
            lines.append("### Counts")
            for kind in sorted(counter):
                label = KIND_LABELS.get(kind, kind)
                lines.append(f"- `{label}`: {counter[kind]}")
            lines.append("")

        recent = sorted(rows, key=lambda item: float(item["mtime"] or 0.0), reverse=True)[:5]
        if recent:
            lines.append("### Recent Pages")
            for row in recent:
                summary = str(row["summary"] or row["title"])
                lines.append(
                    f"- [{row['title']}]({rel_link(root, str(row['path']))}) | `{row['memory_kind']}` | {summary}"
                )
            lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_sources(raw_events: list[dict[str, object]]) -> str:
    counts = Counter(str(row["source_type"] or "unknown") for row in raw_events)
    states = Counter(str(row["processed_state"] or "unknown") for row in raw_events)
    lines = [
        "# Sources",
        "",
        "这份视图描述原始来源层，而不是正式记忆页。",
        "原则：`raw_events` 保留原始记录，正式记忆页只保存整理后的结论，并通过 `related_sources` 指回来源。",
        "",
        "## Source Types",
    ]
    if not counts:
        lines.append("- 当前没有原始来源。")
    else:
        for source_type, count in sorted(counts.items()):
            lines.append(f"- `{source_type}`: {count}")

    lines.extend(["", "## Processing States"])
    if not states:
        lines.append("- 当前没有处理状态。")
    else:
        for state, count in sorted(states.items()):
            lines.append(f"- `{state}`: {count}")

    lines.extend(["", "## Layering Rules", ""])
    lines.append("- 用户/助手对话默认先进入原始事件层，再整理到 `session` 或 `candidate`。")
    lines.append("- 长期层默认通过显式 `remember` 或受控 artifact capture 进入。")
    lines.append("- `index.md` 看正式记忆页，`log.md` 看原始时间线，`sources.md` 看来源层规则和分布。")
    return "\n".join(lines).strip() + "\n"


def render_log(root: Path, raw_events: list[dict[str, object]]) -> str:
    lines = [
        "# Memory Log",
        "",
        "这份时间线是原始事件层的最近活动，不等于正式记忆。",
        "",
    ]
    if not raw_events:
        lines.append("当前还没有原始事件。")
        return "\n".join(lines).strip() + "\n"

    for row in raw_events:
        target = str(row["target_memory_path"] or "").strip()
        target_text = f" -> [{Path(target).name}]({rel_link(root, target)})" if target else ""
        snippet = str(row["content"] or "").replace("\n", " ").strip()
        if len(snippet) > 120:
            snippet = snippet[:117] + "..."
        lines.append(
            f"- {row['created_at']} | `{row['subject_id']}` | `{row['source_type']}` | `{row['processed_state']}` | `{row['target_memory_kind'] or ''}`{target_text} | {snippet}"
        )
    return "\n".join(lines).strip() + "\n"


def main() -> None:
    args = parse_args()
    root = store_root(args.store)
    conn = open_db(root)

    doc_rows = conn.execute(
        """
        SELECT
            path, title, subject_id, subject_name, memory_kind, page_role, canonical, summary, mtime
        FROM documents
        ORDER BY subject_id, memory_kind, title
        """
    ).fetchall()
    docs = [
        {
            "path": raw[0],
            "title": raw[1],
            "subject_id": raw[2],
            "subject_name": raw[3],
            "memory_kind": raw[4],
            "page_role": raw[5],
            "canonical": raw[6],
            "summary": raw[7],
            "mtime": raw[8],
        }
        for raw in doc_rows
    ]

    raw_rows = conn.execute(
        """
        SELECT
            subject_id, source_type, processed_state, target_memory_kind, target_memory_path, content, created_at
        FROM raw_events
        ORDER BY id DESC
        LIMIT ?
        """,
        (args.recent_events,),
    ).fetchall()
    raw_events = [
        {
            "subject_id": raw[0],
            "source_type": raw[1],
            "processed_state": raw[2],
            "target_memory_kind": raw[3],
            "target_memory_path": raw[4],
            "content": raw[5],
            "created_at": raw[6],
        }
        for raw in raw_rows
    ]
    conn.close()

    (root / "index.md").write_text(render_index(root, docs), encoding="utf-8")
    (root / "log.md").write_text(render_log(root, raw_events), encoding="utf-8")
    (root / "sources.md").write_text(render_sources(raw_events), encoding="utf-8")

    emit(
        {
            "status": "ok",
            "store": str(root),
            "views": [
                str(root / "index.md"),
                str(root / "log.md"),
                str(root / "sources.md"),
            ],
        }
    )


if __name__ == "__main__":
    main()
