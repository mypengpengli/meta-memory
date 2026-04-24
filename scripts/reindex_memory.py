#!/usr/bin/env python3
from __future__ import annotations

import re
import sqlite3

from _common import (
    as_float,
    emit,
    first_heading,
    first_summary_line,
    indexed_roots,
    json_text,
    markdown_files,
    open_db,
    parse_args,
    parse_frontmatter,
    read_text,
    split_frontmatter,
    store_root,
)


def infer_memory_kind(meta: dict[str, object], path: str) -> str:
    kind = str(meta.get("memory_kind") or meta.get("scope") or meta.get("type") or "").strip()
    if kind:
        return kind
    normalized = path.replace("\\", "/")
    if "/profile/" in normalized or "/fixed/" in normalized:
        return "profile"
    if "/states/" in normalized:
        return "state"
    if "/events/" in normalized:
        return "event"
    if "/relationships/" in normalized:
        return "relationship"
    if "/goals/" in normalized or "/projects/" in normalized:
        return "goal"
    if "/domains/" in normalized or "/topics/" in normalized:
        return "domain"
    if "/sessions/" in normalized:
        return "session"
    if "/candidates/" in normalized:
        return "candidate"
    return "note"


def infer_page_role(meta: dict[str, object], path: str, memory_kind: str) -> str:
    role = str(meta.get("page_role", "")).strip()
    if role:
        return role
    normalized = path.replace("\\", "/")
    if normalized.endswith("/index.md"):
        return "index"
    if normalized.endswith("/log.md"):
        return "log"
    if normalized.endswith("/sources.md"):
        return "sources"
    if memory_kind == "profile":
        return "profile-note"
    if memory_kind == "state":
        return "state-note"
    if memory_kind == "goal":
        return "goal-note"
    if memory_kind == "relationship":
        return "relationship-note"
    if memory_kind == "event":
        return "event-note"
    if memory_kind == "domain":
        return "domain-note"
    if memory_kind == "session":
        return "session-note"
    if memory_kind == "candidate":
        return "candidate-note"
    return "note"


def infer_domain(meta: dict[str, object], path: str) -> str:
    domain = str(meta.get("domain") or meta.get("area") or "").strip()
    if domain:
        return domain
    normalized = path.replace("\\", "/")
    for candidate in ["work", "learning", "daily-life", "health", "finance", "relationships"]:
        if f"/{candidate}/" in normalized or normalized.endswith(f"/{candidate}.md"):
            return candidate
    return "general"


def parse_list_text(value: object) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value if str(item).strip())
    return str(value or "")


def search_terms(text: str) -> list[str]:
    terms: set[str] = set()
    normalized = text.casefold()
    for token in re.findall(r"[a-z0-9][a-z0-9_\-./]+", normalized):
        if len(token) >= 2:
            terms.add(token)
            for piece in re.split(r"[\-./_]+", token):
                if len(piece) >= 2:
                    terms.add(piece)
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        terms.add(run)
        for width in (2, 3):
            if len(run) >= width:
                for idx in range(0, len(run) - width + 1):
                    terms.add(run[idx : idx + width])
    return sorted(terms)


def ensure_fts(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
                path UNINDEXED,
                title,
                topic,
                tags,
                summary,
                related_people,
                related_events,
                related_topics,
                related_sources,
                content
            )
            """
        )
        conn.execute("DELETE FROM document_fts")
    except sqlite3.OperationalError:
        return False
    return True


def main() -> None:
    args = parse_args("Reindex person memory notes into SQLite.")
    root = store_root(args.store)
    conn = open_db(root)
    fts_enabled = ensure_fts(conn)
    files = markdown_files(indexed_roots(root))
    current_paths = {str(path) for path in files}

    if current_paths:
        placeholders = ", ".join("?" for _ in current_paths)
        conn.execute(
            f"DELETE FROM documents WHERE path NOT IN ({placeholders})",
            tuple(sorted(current_paths)),
        )
        conn.execute(
            f"DELETE FROM scores WHERE path NOT IN ({placeholders})",
            tuple(sorted(current_paths)),
        )
    else:
        conn.execute("DELETE FROM documents")
        conn.execute("DELETE FROM scores")

    indexed = 0
    for path in files:
        text = read_text(path)
        meta, body = split_frontmatter(text)
        if not meta:
            meta = parse_frontmatter(path)
        doc_path = str(path)
        title = first_heading(path)
        summary = first_summary_line(path)
        memory_kind = infer_memory_kind(meta, doc_path)
        confidence = as_float(meta.get("confidence"), 0.5 if memory_kind != "candidate" else 0.3)
        importance = as_float(meta.get("importance"), 0.5)
        conn.execute(
            """
            INSERT INTO documents(
                path, title, subject_id, subject_name, memory_kind, page_role, canonical, domain, topic, tags, summary,
                confidence, importance, status, source, start_at, end_at, related_people, related_events,
                related_topics, related_sources, supersedes, replaced_by, mtime
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                title=excluded.title,
                subject_id=excluded.subject_id,
                subject_name=excluded.subject_name,
                memory_kind=excluded.memory_kind,
                page_role=excluded.page_role,
                canonical=excluded.canonical,
                domain=excluded.domain,
                topic=excluded.topic,
                tags=excluded.tags,
                summary=excluded.summary,
                confidence=excluded.confidence,
                importance=excluded.importance,
                status=excluded.status,
                source=excluded.source,
                start_at=excluded.start_at,
                end_at=excluded.end_at,
                related_people=excluded.related_people,
                related_events=excluded.related_events,
                related_topics=excluded.related_topics,
                related_sources=excluded.related_sources,
                supersedes=excluded.supersedes,
                replaced_by=excluded.replaced_by,
                mtime=excluded.mtime
            """,
            (
                doc_path,
                title,
                str(meta.get("subject_id", "")),
                str(meta.get("subject_name", "")),
                memory_kind,
                infer_page_role(meta, doc_path, memory_kind),
                1 if bool(meta.get("canonical", False)) else 0,
                infer_domain(meta, doc_path),
                str(meta.get("topic", "")),
                json_text(meta.get("tags", [])),
                summary,
                confidence,
                importance,
                str(meta.get("status", "pending" if memory_kind == "candidate" else "active")),
                json_text(meta.get("source", meta.get("related_sources", ""))),
                str(meta.get("start_at", "")),
                str(meta.get("end_at", "")),
                json_text(meta.get("related_people", [])),
                json_text(meta.get("related_events", [])),
                json_text(meta.get("related_topics", [])),
                json_text(meta.get("related_sources", [])),
                json_text(meta.get("supersedes", [])),
                json_text(meta.get("replaced_by", [])),
                path.stat().st_mtime,
            ),
        )
        conn.execute(
            """
            INSERT INTO scores(path, confidence)
            VALUES(?, ?)
            ON CONFLICT(path) DO UPDATE SET confidence=excluded.confidence
            """,
            (doc_path, confidence),
        )
        if fts_enabled:
            related_sources = parse_list_text(meta.get("related_sources", []))
            content_terms = " ".join(
                search_terms(
                    "\n".join(
                        [
                            title,
                            summary,
                            body,
                            str(meta.get("topic", "")),
                            parse_list_text(meta.get("tags", [])),
                            parse_list_text(meta.get("related_people", [])),
                            parse_list_text(meta.get("related_events", [])),
                            parse_list_text(meta.get("related_topics", [])),
                            related_sources,
                        ]
                    )
                )
            )
            conn.execute(
                """
                INSERT INTO document_fts(
                    path, title, topic, tags, summary, related_people, related_events,
                    related_topics, related_sources, content
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc_path,
                    title,
                    str(meta.get("topic", "")),
                    parse_list_text(meta.get("tags", [])),
                    summary,
                    parse_list_text(meta.get("related_people", [])),
                    parse_list_text(meta.get("related_events", [])),
                    parse_list_text(meta.get("related_topics", [])),
                    related_sources,
                    content_terms,
                ),
            )
        indexed += 1
    conn.commit()
    conn.close()
    emit({"status": "ok", "indexed": indexed, "fts_enabled": fts_enabled, "store": str(root)})


if __name__ == "__main__":
    main()
