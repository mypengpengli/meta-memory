"""Scoped, source-linked Dream digests that never overwrite factual claims."""
from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig
from .legacy import bootstrap


def _hash_claims(claims: list[dict[str, object]]) -> str:
    payload = [(item["id"], item.get("content_hash", ""), item.get("updated_at", "")) for item in claims]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _lines(claims: list[dict[str, object]], *, limit: int = 8) -> list[str]:
    lines: list[str] = []
    for item in claims[:limit]:
        observed = str(item.get("verification_state") or "").strip().casefold().replace("-", "_") == "agent_observed"
        provenance = " [Agent-observed]" if observed else ""
        lines.append(f"- {item['content']}{provenance} [claim:{item['id']}]")
    return lines


def _upsert_node(
    conn,
    *,
    profile_id: str,
    workspace_id: str,
    subject_id: str,
    node_type: str,
    title: str,
    content: str,
    source_claim_ids: list[str],
    source_hash: str,
    confidence: float = 0.8,
    inference_level: str = "extractive",
    status: str = "active",
    prompt_eligible: bool = True,
) -> str:
    existing = conn.execute(
        "SELECT dream_uid FROM dream_nodes WHERE profile_id=? AND workspace_id=? AND subject_id=? AND node_type=? AND source_hash=?",
        (profile_id, workspace_id, subject_id, node_type, source_hash),
    ).fetchone()
    uid = str(existing[0]) if existing else str(uuid.uuid4())
    if existing:
        conn.execute(
            """
            UPDATE dream_nodes SET title=?,content=?,source_claim_ids=?,confidence=?,inference_level=?,status=?,prompt_eligible=?,updated_at=CURRENT_TIMESTAMP
            WHERE dream_uid=?
            """,
            (title, content, json.dumps(source_claim_ids, ensure_ascii=False), confidence, inference_level, status, int(prompt_eligible), uid),
        )
    else:
        conn.execute(
            """
            INSERT INTO dream_nodes(
                dream_uid,profile_id,workspace_id,subject_id,visibility_scope,node_type,title,content,
                source_claim_ids,source_hash,confidence,inference_level,status,prompt_eligible
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (uid, profile_id, workspace_id, subject_id, "global" if workspace_id == "global" else "workspace", node_type, title, content, json.dumps(source_claim_ids, ensure_ascii=False), source_hash, confidence, inference_level, status, int(prompt_eligible)),
        )
    return uid


def _scope_claims(conn, *, profile_id: str, workspace_id: str, subject_id: str, cutoff: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT id,memory_kind,topic,content,confidence,importance,content_hash,updated_at,created_at,predicate,
               verification_state
        FROM claims
        WHERE profile_id=? AND workspace_id=? AND subject_id=? AND status='active'
          AND COALESCE(prompt_eligible, 0)=1
          AND COALESCE(visibility_scope, 'workspace') IN ('global', 'workspace')
          AND LOWER(COALESCE(memory_kind, '')) != 'resource'
          AND LOWER(COALESCE(verification_state, '')) != 'resource'
          AND security_state NOT IN ('blocked','suspicious')
          -- Current states/goals/relationships remain part of a usable Dream
          -- even if they have not changed during this scan window.  Only
          -- event-like/history material is deliberately recency bounded.
          AND (
                LOWER(COALESCE(memory_kind, '')) IN ('profile','state','goal','relationship')
                OR updated_at>=? OR created_at>=?
          )
        ORDER BY importance DESC,updated_at DESC
        LIMIT 200
        """,
        (profile_id, workspace_id, subject_id, cutoff, cutoff),
    ).fetchall()
    keys = ["id", "kind", "topic", "content", "confidence", "importance", "content_hash", "updated_at", "created_at", "predicate", "verification_state"]
    return [dict(zip(keys, row)) for row in rows]


def _scopes(conn, config: AppConfig) -> list[tuple[str, str]]:
    rows = conn.execute(
        """
        SELECT DISTINCT workspace_id,subject_id FROM claims
        WHERE profile_id=?
          AND COALESCE(prompt_eligible, 0)=1
          AND COALESCE(visibility_scope, 'workspace') IN ('global', 'workspace')
          AND LOWER(COALESCE(memory_kind, '')) != 'resource'
          AND LOWER(COALESCE(verification_state, '')) != 'resource'
        UNION
        SELECT DISTINCT workspace_id,subject_id FROM workspace_runtime_state WHERE profile_id=? AND agent_id=''
        """,
        (config.profile_id, config.profile_id),
    ).fetchall()
    scopes = {(str(row[0]), str(row[1])) for row in rows if str(row[0] or "") and str(row[1] or "")}
    scopes.add(("global", config.subject_id))
    return sorted(scopes)


def _open_questions(conn, *, profile_id: str, workspace_id: str, subject_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT action,summary,proposal_uid FROM write_proposals
        WHERE profile_id=? AND workspace_id=? AND subject_id=? AND status IN ('pending','needs_clarification')
        ORDER BY created_at DESC LIMIT 8
        """,
        (profile_id, workspace_id, subject_id),
    ).fetchall()
    return [f"- {row[0] or 'memory'}: {row[1] or row[2]}" for row in rows]


def _mark_dream_clean(conn, *, profile_id: str, workspace_id: str, subject_id: str) -> None:
    row = conn.execute(
        "SELECT claim_generation FROM workspace_runtime_state WHERE profile_id=? AND workspace_id=? AND subject_id=? AND agent_id=''",
        (profile_id, workspace_id, subject_id),
    ).fetchone()
    if not row:
        return
    generation = int(row[0] or 0)
    conn.execute(
        """
        UPDATE workspace_runtime_state
        SET dream_dirty=CASE WHEN claim_generation=? THEN 0 ELSE 1 END,
            dream_generation=CASE WHEN claim_generation=? THEN ? ELSE dream_generation END,
            last_maintained_at=CURRENT_TIMESTAMP,last_success_at=CURRENT_TIMESTAMP,last_error=NULL,updated_at=CURRENT_TIMESTAMP
        WHERE profile_id=? AND workspace_id=? AND subject_id=? AND agent_id=''
        """,
        (generation, generation, generation, profile_id, workspace_id, subject_id),
    )


def run_dream(config: AppConfig, *, scan_days: int | None = None) -> dict[str, Any]:
    """Generate scoped extractive digests and optional non-prompt inferences."""
    bootstrap()
    from _common import ensure_store_ready, open_db, utc_now

    root = Path(config.store)
    ensure_store_ready(root)
    days = max(1, scan_days or config.dream_scan_days)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    run_uid = str(uuid.uuid4())
    conn = open_db(root)
    provider = config.dream_provider if config.dream_provider in {"deterministic", "command"} else "deterministic"
    conn.execute(
        "INSERT INTO dream_runs(run_uid,profile_id,started_at,status,provider,scan_days) VALUES(?, ?, ?, 'running', ?, ?)",
        (run_uid, config.profile_id, utc_now(), provider, days),
    )
    reports: list[Path] = []
    nodes: list[dict[str, object]] = []
    all_source_ids: set[str] = set()
    try:
        for workspace_id, subject_id in _scopes(conn, config):
            claims = _scope_claims(conn, profile_id=config.profile_id, workspace_id=workspace_id, subject_id=subject_id, cutoff=cutoff)
            source_ids = [str(item["id"]) for item in claims]
            all_source_ids.update(source_ids)
            source_hash = _hash_claims(claims)
            by_kind: dict[str, list[dict[str, object]]] = {}
            for claim in claims:
                by_kind.setdefault(str(claim["kind"]), []).append(claim)
            rendered: list[tuple[str, str, str, list[str], bool, str]] = []
            if workspace_id == "global":
                user_claims = [item for item in claims if item["kind"] in {"profile", "goal", "relationship"}]
                rendered.append(("user_summary", "User Summary", "\n".join(_lines(user_claims) or ["- No sourced user summary is available."]), [str(item["id"]) for item in user_claims], True, "extractive"))
            digest_claims = [item for item in claims if item["kind"] in {"state", "goal", "event", "domain"}]
            rendered.append(("project_digest", "Project Digest", "\n".join(_lines(digest_claims) or ["- No recent sourced project digest is available."]), [str(item["id"]) for item in digest_claims], True, "extractive"))
            procedures = [item for item in claims if str(item.get("predicate") or "") == "procedure" or ("先" in str(item["content"]) and ("再" in str(item["content"]) or "排查" in str(item["content"])))]
            if procedures:
                rendered.append(("procedure_candidate", "Procedure Candidate", "\n".join(_lines(procedures)), [str(item["id"]) for item in procedures], False, "extractive"))
            questions = _open_questions(conn, profile_id=config.profile_id, workspace_id=workspace_id, subject_id=subject_id)
            if questions:
                rendered.append(("open_question", "Open Questions", "\n".join(questions), [], False, "extractive"))

            if provider == "command" and claims:
                from .dream_provider import command_synthesize

                semantic = command_synthesize(config.dream_command, {"workspace_id": workspace_id, "claims": claims, "procedures": procedures, "open_conflicts": questions})
                for text in semantic["project_digest"]:
                    rendered.append(("project_digest", "Inferred Project Digest", text, source_ids, False, "inferred"))
                for text in semantic["patterns"]:
                    rendered.append(("pattern", "Inferred Pattern", text, source_ids, False, "inferred"))
                for text in semantic["procedure_candidates"]:
                    rendered.append(("procedure_candidate", "Inferred Procedure Candidate", text, source_ids, False, "inferred"))
                for text in semantic["open_questions"]:
                    rendered.append(("open_question", "Inferred Open Question", text, source_ids, False, "inferred"))

            report_lines = ["---", "schema_version: 2", f"run_uid: {run_uid}", f"workspace_id: {workspace_id}", f"subject_id: {subject_id}", f"generated_at: {utc_now()}", f"source_claim_ids: {source_ids}", "---", "", "# Dream report", "", "This is deferred source-linked synthesis, not a replacement for claims."]
            for node_type, title, content, node_sources, eligible, level in rendered:
                node_hash = hashlib.sha256(f"{source_hash}:{node_type}:{content}".encode("utf-8")).hexdigest()
                uid = _upsert_node(conn, profile_id=config.profile_id, workspace_id=workspace_id, subject_id=subject_id, node_type=node_type, title=title, content=content, source_claim_ids=node_sources, source_hash=node_hash, confidence=0.75 if level == "extractive" else 0.4, inference_level=level, status="active" if level == "extractive" else "inferred", prompt_eligible=eligible and level == "extractive")
                nodes.append({"id": uid, "workspace_id": workspace_id, "subject_id": subject_id, "type": node_type, "prompt_eligible": eligible and level == "extractive"})
                report_lines.extend(["", f"## {title}", content])
            report_dir = root / "dream" / workspace_id.replace(":", "-")
            report_dir.mkdir(parents=True, exist_ok=True)
            report = report_dir / f"dream-{run_uid}.md"
            report.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
            reports.append(report)
            _mark_dream_clean(conn, profile_id=config.profile_id, workspace_id=workspace_id, subject_id=subject_id)
        conn.execute("UPDATE dream_runs SET status='completed',completed_at=?,last_error=NULL WHERE run_uid=?", (utc_now(), run_uid))
        conn.commit()
    except Exception as exc:
        conn.execute("UPDATE dream_runs SET status='failed',completed_at=?,last_error=? WHERE run_uid=?", (utc_now(), str(exc)[:2000], run_uid))
        conn.commit()
        raise
    finally:
        conn.close()
    return {"status": "ok", "run_id": run_uid, "report": str(reports[0]) if reports else "", "reports": [str(path) for path in reports], "inferred": True, "nodes": nodes, "source_claim_ids": sorted(all_source_ids), "repeated_topics": [], "open_questions": sum(1 for node in nodes if node["type"] == "open_question")}
