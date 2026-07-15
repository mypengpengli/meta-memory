"""Scoped, source-linked Dream digests with a real no-op lifecycle.

Dream is a deferred derivative of Claims.  It never replaces a Claim, and an
unchanged (or source-empty) scope never creates a report or prompt-visible
node merely to say that it has nothing to say.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid
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
    run_uid: str = "",
    confidence: float = 0.8,
    inference_level: str = "extractive",
    status: str = "active",
    prompt_eligible: bool = True,
) -> str:
    # A Dream node without Claim sources is not meaningful provenance.  This
    # guard is intentionally here as well as in the renderer, so future
    # callers cannot accidentally create an empty prompt-visible node.
    if not source_claim_ids or not str(content or "").strip():
        raise ValueError("Dream nodes require non-empty content and source Claim ids.")
    existing = conn.execute(
        "SELECT dream_uid FROM dream_nodes WHERE profile_id=? AND workspace_id=? AND subject_id=? AND node_type=? AND source_hash=?",
        (profile_id, workspace_id, subject_id, node_type, source_hash),
    ).fetchone()
    uid = str(existing[0]) if existing else str(uuid.uuid4())
    if existing:
        conn.execute(
            """
            UPDATE dream_nodes SET title=?,content=?,source_claim_ids=?,confidence=?,inference_level=?,status=?,prompt_eligible=?,last_run_uid=?,updated_at=CURRENT_TIMESTAMP
            WHERE dream_uid=?
            """,
            (title, content, json.dumps(source_claim_ids, ensure_ascii=False), confidence, inference_level, status, int(prompt_eligible), run_uid or None, uid),
        )
    else:
        conn.execute(
            """
            INSERT INTO dream_nodes(
                dream_uid,profile_id,workspace_id,subject_id,visibility_scope,node_type,title,content,
                source_claim_ids,source_hash,confidence,inference_level,status,prompt_eligible,last_run_uid
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uid, profile_id, workspace_id, subject_id,
                "global" if workspace_id == "global" else "workspace",
                node_type, title, content, json.dumps(source_claim_ids, ensure_ascii=False), source_hash,
                confidence, inference_level, status, int(prompt_eligible), run_uid or None,
            ),
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
    # Do not add a synthetic global scope.  A source-empty Dream is a true
    # no-op and must not emit a "No sourced ..." report/node.
    rows = conn.execute(
        """
        SELECT DISTINCT workspace_id,subject_id FROM claims
        WHERE profile_id=?
          AND COALESCE(prompt_eligible, 0)=1
          AND COALESCE(visibility_scope, 'workspace') IN ('global', 'workspace')
          AND LOWER(COALESCE(memory_kind, '')) != 'resource'
          AND LOWER(COALESCE(verification_state, '')) != 'resource'
        """,
        (config.profile_id,),
    ).fetchall()
    return sorted({(str(row[0]), str(row[1])) for row in rows if str(row[0] or "") and str(row[1] or "")})


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


def _previous_hash(conn, *, profile_id: str, workspace_id: str, subject_id: str) -> str:
    row = conn.execute(
        "SELECT source_hash FROM dream_scope_state WHERE profile_id=? AND workspace_id=? AND subject_id=?",
        (profile_id, workspace_id, subject_id),
    ).fetchone()
    return str(row[0] or "") if row else ""


def _scope_plan(conn, config: AppConfig, *, workspace_id: str, subject_id: str, cutoff: str) -> dict[str, object]:
    claims = _scope_claims(conn, profile_id=config.profile_id, workspace_id=workspace_id, subject_id=subject_id, cutoff=cutoff)
    source_ids = [str(item["id"]) for item in claims]
    source_hash = _hash_claims(claims) if claims else ""
    previous_hash = _previous_hash(conn, profile_id=config.profile_id, workspace_id=workspace_id, subject_id=subject_id)
    return {
        "workspace_id": workspace_id,
        "subject_id": subject_id,
        "claims": claims,
        "source_claim_ids": source_ids,
        "source_hash": source_hash,
        "previous_source_hash": previous_hash,
        "changed": bool(source_ids) and source_hash != previous_hash,
        "reason": "source_empty" if not source_ids else "source_unchanged" if source_hash == previous_hash else "source_changed",
    }


def _render_scope(conn, config: AppConfig, plan: dict[str, object], *, provider: str) -> list[tuple[str, str, str, list[str], bool, str]]:
    """Render only non-empty source-linked nodes for one changed scope."""

    workspace_id = str(plan["workspace_id"])
    subject_id = str(plan["subject_id"])
    claims = list(plan["claims"])
    source_ids = list(plan["source_claim_ids"])
    by_kind: dict[str, list[dict[str, object]]] = {}
    for claim in claims:
        by_kind.setdefault(str(claim["kind"]), []).append(claim)
    rendered: list[tuple[str, str, str, list[str], bool, str]] = []
    if workspace_id == "global":
        user_claims = [item for item in claims if item["kind"] in {"profile", "goal", "relationship"}]
        if user_claims:
            rendered.append(("user_summary", "User Summary", "\n".join(_lines(user_claims)), [str(item["id"]) for item in user_claims], True, "extractive"))
    digest_claims = [item for item in claims if item["kind"] in {"state", "goal", "event", "domain", "relationship", "profile"}]
    # A scope with valid Claims always has a source-linked digest.  This is a
    # fallback rather than an empty placeholder when the narrower type filter
    # happens to find nothing.
    digest_claims = digest_claims or claims
    if digest_claims:
        rendered.append(("project_digest", "Project Digest", "\n".join(_lines(digest_claims)), [str(item["id"]) for item in digest_claims], True, "extractive"))
    procedures = [item for item in claims if str(item.get("predicate") or "") == "procedure" or ("先" in str(item["content"]) and ("再" in str(item["content"]) or "排查" in str(item["content"])))]
    if procedures:
        rendered.append(("procedure_candidate", "Procedure Candidate", "\n".join(_lines(procedures)), [str(item["id"]) for item in procedures], False, "extractive"))
    questions = _open_questions(conn, profile_id=config.profile_id, workspace_id=workspace_id, subject_id=subject_id)
    if questions:
        # Questions are linked to the scope source set, never represented as
        # source-less prompt nodes.
        rendered.append(("open_question", "Open Questions", "\n".join(questions), source_ids, False, "extractive"))
    if provider == "command" and claims:
        from .dream_provider import command_synthesize

        semantic = command_synthesize(config.dream_command, {"workspace_id": workspace_id, "claims": claims, "procedures": procedures, "open_conflicts": questions})
        for text in semantic.get("project_digest", []):
            text = str(text or "").strip()
            if text:
                rendered.append(("project_digest", "Inferred Project Digest", text, source_ids, False, "inferred"))
        for text in semantic.get("patterns", []):
            text = str(text or "").strip()
            if text:
                rendered.append(("pattern", "Inferred Pattern", text, source_ids, False, "inferred"))
        for text in semantic.get("procedure_candidates", []):
            text = str(text or "").strip()
            if text:
                rendered.append(("procedure_candidate", "Inferred Procedure Candidate", text, source_ids, False, "inferred"))
        for text in semantic.get("open_questions", []):
            text = str(text or "").strip()
            if text:
                rendered.append(("open_question", "Inferred Open Question", text, source_ids, False, "inferred"))
    return [item for item in rendered if item[2].strip() and item[3]]


def _plans(conn, config: AppConfig, *, scan_days: int) -> list[dict[str, object]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=scan_days)).isoformat()
    return [_scope_plan(conn, config, workspace_id=workspace_id, subject_id=subject_id, cutoff=cutoff) for workspace_id, subject_id in _scopes(conn, config)]


def preview_dream(config: AppConfig, *, scan_days: int | None = None) -> dict[str, Any]:
    """Preview changed Dream scopes without creating runs, nodes, or reports."""

    bootstrap()
    from _common import ensure_store_ready, open_db

    root = Path(config.store)
    ensure_store_ready(root)
    days = max(1, scan_days or config.dream_scan_days)
    conn = open_db(root)
    try:
        plans = _plans(conn, config, scan_days=days)
    finally:
        conn.close()
    changed = [plan for plan in plans if bool(plan["changed"])]
    return {
        "status": "preview",
        "scan_days": days,
        "would_run": bool(changed),
        "changed_scopes": [
            {key: plan[key] for key in ("workspace_id", "subject_id", "source_claim_ids", "source_hash", "previous_source_hash", "reason")}
            for plan in changed
        ],
        "idle_scopes": [
            {key: plan[key] for key in ("workspace_id", "subject_id", "source_claim_ids", "source_hash", "previous_source_hash", "reason")}
            for plan in plans if not bool(plan["changed"])
        ],
    }


def _report_path(root: Path, *, workspace_id: str, run_uid: str) -> Path:
    return root / "dream" / workspace_id.replace(":", "-") / f"dream-{run_uid}.md"


def _write_report(root: Path, *, run_uid: str, plan: dict[str, object], rendered: list[tuple[str, str, str, list[str], bool, str]], generated_at: str) -> Path:
    workspace_id = str(plan["workspace_id"])
    report = _report_path(root, workspace_id=workspace_id, run_uid=run_uid)
    report.parent.mkdir(parents=True, exist_ok=True)
    report_lines = [
        "---", "schema_version: 3", f"run_uid: {run_uid}", f"workspace_id: {workspace_id}",
        f"subject_id: {plan['subject_id']}", f"generated_at: {generated_at}",
        f"source_hash: {plan['source_hash']}", f"source_claim_ids: {plan['source_claim_ids']}", "---", "",
        "# Dream report", "", "This is deferred source-linked synthesis, not a replacement for Claims.",
    ]
    for _, title, content, _, _, _ in rendered:
        report_lines.extend(["", f"## {title}", content])
    report.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    return report


def _record_scope_state(conn, *, config: AppConfig, plan: dict[str, object], run_uid: str, completed_at: str) -> None:
    conn.execute(
        """
        INSERT INTO dream_scope_state(profile_id,workspace_id,subject_id,source_hash,last_run_uid,last_completed_at,updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(profile_id,workspace_id,subject_id) DO UPDATE SET
            source_hash=excluded.source_hash,last_run_uid=excluded.last_run_uid,
            last_completed_at=excluded.last_completed_at,updated_at=excluded.updated_at
        """,
        (config.profile_id, plan["workspace_id"], plan["subject_id"], plan["source_hash"], run_uid, completed_at, completed_at),
    )


def run_dream(config: AppConfig, *, scan_days: int | None = None) -> dict[str, Any]:
    """Generate changed, non-empty scoped Dream digests.

    Unlike the previous implementation, a no-source or unchanged scope returns
    ``idle`` without creating a Dream run, report, or prompt-eligible node.
    """

    bootstrap()
    from _common import ensure_store_ready, open_db, utc_now

    root = Path(config.store)
    ensure_store_ready(root)
    days = max(1, scan_days or config.dream_scan_days)
    conn = open_db(root)
    provider = config.dream_provider if config.dream_provider in {"deterministic", "command"} else "deterministic"
    reports: list[Path] = []
    nodes: list[dict[str, object]] = []
    try:
        plans = _plans(conn, config, scan_days=days)
        changed = [plan for plan in plans if bool(plan["changed"])]
        if not changed:
            return {
                "status": "idle",
                "scan_days": days,
                "reason": "no_changed_sourced_claims",
                "run_id": "",
                "reports": [],
                "nodes": [],
                "scopes": [{key: plan[key] for key in ("workspace_id", "subject_id", "reason")} for plan in plans],
            }
        run_uid = str(uuid.uuid4())
        started_at = utc_now()
        conn.execute(
            "INSERT INTO dream_runs(run_uid,profile_id,workspace_id,subject_id,started_at,status,provider,scan_days) VALUES(?, ?, '*', ?, ?, 'running', ?, ?)",
            (run_uid, config.profile_id, config.subject_id, started_at, provider, days),
        )
        all_source_ids: set[str] = set()
        for plan in changed:
            rendered = _render_scope(conn, config, plan, provider=provider)
            # A changed plan is normally renderable because it has Claims.  If
            # a future renderer removes every type, still do not create an
            # empty report; leave it dirty for an explicit future renderer.
            if not rendered:
                continue
            for node_type, title, content, node_sources, eligible, level in rendered:
                node_hash = hashlib.sha256(f"{plan['source_hash']}:{node_type}:{content}".encode("utf-8")).hexdigest()
                uid = _upsert_node(
                    conn,
                    profile_id=config.profile_id,
                    workspace_id=str(plan["workspace_id"]),
                    subject_id=str(plan["subject_id"]),
                    node_type=node_type,
                    title=title,
                    content=content,
                    source_claim_ids=node_sources,
                    source_hash=node_hash,
                    run_uid=run_uid,
                    confidence=0.75 if level == "extractive" else 0.4,
                    inference_level=level,
                    status="active" if level == "extractive" else "inferred",
                    prompt_eligible=bool(eligible and level == "extractive" and node_sources),
                )
                nodes.append({
                    "id": uid, "workspace_id": plan["workspace_id"], "subject_id": plan["subject_id"],
                    "type": node_type, "prompt_eligible": bool(eligible and level == "extractive" and node_sources),
                })
            generated_at = utc_now()
            report = _write_report(root, run_uid=run_uid, plan=plan, rendered=rendered, generated_at=generated_at)
            reports.append(report)
            conn.execute(
                """
                INSERT OR REPLACE INTO dream_run_reports(run_uid,profile_id,workspace_id,subject_id,source_hash,report_path,node_count,archived_at,created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (run_uid, config.profile_id, plan["workspace_id"], plan["subject_id"], plan["source_hash"], str(report), len(rendered), generated_at),
            )
            _record_scope_state(conn, config=config, plan=plan, run_uid=run_uid, completed_at=generated_at)
            _mark_dream_clean(conn, profile_id=config.profile_id, workspace_id=str(plan["workspace_id"]), subject_id=str(plan["subject_id"]))
            all_source_ids.update(str(item) for item in plan["source_claim_ids"])
        completed_at = utc_now()
        status = "completed" if reports else "idle"
        conn.execute("UPDATE dream_runs SET status=?,completed_at=?,last_error=NULL WHERE run_uid=?", (status, completed_at, run_uid))
        conn.commit()
        return {
            "status": "ok" if reports else "idle",
            "run_id": run_uid,
            "report": str(reports[0]) if reports else "",
            "reports": [str(path) for path in reports],
            # Historical public callers use this as a "Dream synthesis ran"
            # marker, not as a provider identity.  Keep that contract for a
            # deterministic extractive run; node-level inference_level still
            # distinguishes any command-provider inferences.
            "inferred": True,
            "nodes": nodes,
            "source_claim_ids": sorted(all_source_ids),
            "repeated_topics": [],
            "open_questions": sum(1 for node in nodes if node["type"] == "open_question"),
        }
    except Exception as exc:
        # A run row exists only after there was actual sourced work.  Preserve
        # its failure state without fabricating one for an empty no-op.
        if "run_uid" in locals():
            conn.execute("UPDATE dream_runs SET status='failed',completed_at=?,last_error=? WHERE run_uid=?", (utc_now(), str(exc)[:2000], run_uid))
            conn.commit()
        raise
    finally:
        conn.close()


def list_dream_runs(config: AppConfig, *, limit: int = 30, include_archived: bool = False) -> dict[str, Any]:
    """List Dream runs and report handles for public CLI presentation."""

    bootstrap()
    from _common import ensure_store_ready, open_db

    root = Path(config.store)
    ensure_store_ready(root)
    conn = open_db(root)
    try:
        rows = conn.execute(
            """
            SELECT r.run_uid,r.started_at,r.completed_at,r.status,r.provider,r.scan_days,r.last_error,
                   p.workspace_id,p.subject_id,p.report_path,p.node_count,p.archived_at,p.source_hash
            FROM dream_runs AS r
            LEFT JOIN dream_run_reports AS p ON p.run_uid=r.run_uid AND p.profile_id=r.profile_id
            WHERE r.profile_id=?
            """ + ("" if include_archived else " AND (p.archived_at IS NULL OR p.run_uid IS NULL)") + " ORDER BY r.started_at DESC LIMIT ?",
            (config.profile_id, max(1, int(limit))),
        ).fetchall()
    finally:
        conn.close()
    grouped: dict[str, dict[str, object]] = {}
    for row in rows:
        uid = str(row[0])
        item = grouped.setdefault(uid, {
            "run_id": uid, "started_at": str(row[1] or ""), "completed_at": str(row[2] or ""), "status": str(row[3] or ""),
            "provider": str(row[4] or ""), "scan_days": int(row[5] or 0), "last_error": str(row[6] or ""), "reports": [],
        })
        if row[7] is not None:
            item["reports"].append({
                "workspace_id": str(row[7] or ""), "subject_id": str(row[8] or ""), "path": str(row[9] or ""),
                "node_count": int(row[10] or 0), "archived_at": str(row[11] or ""), "source_hash": str(row[12] or ""),
            })
    return {"status": "ok", "runs": list(grouped.values())}


def show_dream_run(config: AppConfig, *, run_id: str, include_content: bool = True) -> dict[str, Any]:
    """Show one Dream run plus its bounded report text and source-linked nodes."""

    if not str(run_id or "").strip():
        raise ValueError("A Dream run id is required.")
    bootstrap()
    from _common import ensure_store_ready, open_db

    root = Path(config.store)
    ensure_store_ready(root)
    conn = open_db(root)
    try:
        run = conn.execute(
            "SELECT run_uid,started_at,completed_at,status,provider,scan_days,last_error FROM dream_runs WHERE run_uid=? AND profile_id=?",
            (run_id, config.profile_id),
        ).fetchone()
        if not run:
            raise ValueError("Dream run not found.")
        reports = conn.execute(
            "SELECT workspace_id,subject_id,report_path,node_count,archived_at,source_hash FROM dream_run_reports WHERE run_uid=? AND profile_id=? ORDER BY workspace_id",
            (run_id, config.profile_id),
        ).fetchall()
        nodes = conn.execute(
            """
            SELECT dream_uid,workspace_id,subject_id,node_type,title,content,source_claim_ids,inference_level,status,prompt_eligible,updated_at
            FROM dream_nodes WHERE profile_id=? AND last_run_uid=?
            ORDER BY updated_at,node_type
            """,
            (config.profile_id, run_id),
        ).fetchall()
    finally:
        conn.close()
    report_items = []
    for row in reports:
        path = Path(str(row[2] or ""))
        text = ""
        if include_content and path.is_file():
            text = path.read_text(encoding="utf-8")[:20000]
        report_items.append({
            "workspace_id": str(row[0] or ""), "subject_id": str(row[1] or ""), "path": str(path), "node_count": int(row[3] or 0),
            "archived_at": str(row[4] or ""), "source_hash": str(row[5] or ""), "content": text,
        })
    return {
        "status": "ok",
        "run": {"run_id": str(run[0]), "started_at": str(run[1] or ""), "completed_at": str(run[2] or ""), "status": str(run[3] or ""), "provider": str(run[4] or ""), "scan_days": int(run[5] or 0), "last_error": str(run[6] or "")},
        "reports": report_items,
        "nodes": [
            {"id": str(row[0]), "workspace_id": str(row[1]), "subject_id": str(row[2]), "type": str(row[3]), "title": str(row[4]), "content": str(row[5]), "source_claim_ids": json.loads(str(row[6] or "[]")), "inference_level": str(row[7]), "status": str(row[8]), "prompt_eligible": bool(row[9]), "updated_at": str(row[10] or "")}
            for row in nodes
        ],
    }


def archive_dream_run(config: AppConfig, *, run_id: str) -> dict[str, Any]:
    """Move reports for a completed run under the local Dream archive."""

    if not str(run_id or "").strip():
        raise ValueError("A Dream run id is required.")
    bootstrap()
    from _common import ensure_store_ready, open_db, utc_now

    root = Path(config.store)
    ensure_store_ready(root)
    conn = open_db(root)
    try:
        rows = conn.execute(
            "SELECT workspace_id,subject_id,report_path,archived_at FROM dream_run_reports WHERE run_uid=? AND profile_id=?",
            (run_id, config.profile_id),
        ).fetchall()
        if not rows:
            raise ValueError("Dream run has no archiveable reports.")
        archived: list[dict[str, str]] = []
        timestamp = utc_now()
        for workspace_id, subject_id, stored_path, archived_at in rows:
            source = Path(str(stored_path or ""))
            destination = root / "dream" / "archive" / str(workspace_id).replace(":", "-") / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not str(archived_at or "") and source.is_file():
                if destination.exists():
                    destination = destination.with_name(f"{destination.stem}-{run_id[:8]}{destination.suffix}")
                shutil.move(str(source), str(destination))
            elif str(archived_at or ""):
                destination = source
            conn.execute(
                "UPDATE dream_run_reports SET report_path=?,archived_at=COALESCE(archived_at, ?) WHERE run_uid=? AND profile_id=? AND workspace_id=? AND subject_id=?",
                (str(destination), timestamp, run_id, config.profile_id, workspace_id, subject_id),
            )
            archived.append({"workspace_id": str(workspace_id), "subject_id": str(subject_id), "path": str(destination)})
        conn.commit()
        return {"status": "ok", "run_id": run_id, "archived": archived}
    finally:
        conn.close()
