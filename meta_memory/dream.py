"""Safe nightly synthesis: report inferred patterns without overwriting facts."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig
from .legacy import bootstrap


def run_dream(config: AppConfig, *, scan_days: int | None = None) -> dict[str, Any]:
    """Create an auditable inferred report from recent sourced claims.

    Dream intentionally does not alter existing claims or host instructions.
    It surfaces summaries, repeated topics, procedures and open conflicts for
    later confirmation instead of silently manufacturing user facts.
    """
    bootstrap()
    from _common import ensure_store_ready, open_db, utc_now

    root = Path(config.store)
    ensure_store_ready(root)
    days = max(1, scan_days or config.dream_scan_days)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = open_db(root)
    rows = conn.execute(
        """SELECT id, memory_kind, topic, content, confidence, created_at
           FROM claims WHERE profile_id=? AND created_at>=? AND status IN ('active','candidate')
           ORDER BY created_at DESC LIMIT 200""",
        (config.profile_id, cutoff),
    ).fetchall()
    conflicts = conn.execute(
        "SELECT proposal_uid, action, review_note FROM write_proposals WHERE status IN ('pending','needs_clarification') ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    conn.close()
    claims = [
        {"id": str(row[0]), "kind": str(row[1]), "topic": str(row[2] or "general"), "content": str(row[3]), "confidence": float(row[4] or 0), "created_at": str(row[5])}
        for row in rows
    ]
    topic_counts = Counter(item["topic"] for item in claims)
    repeated = [topic for topic, count in topic_counts.most_common(8) if count >= 2]
    preferences = [item for item in claims if item["kind"] == "profile"][:5]
    procedures = [item for item in claims if "先" in item["content"] and ("再" in item["content"] or "排查" in item["content"])][:5]
    report_lines = [
        "---", "schema_version: 1", f"generated_at: {utc_now()}", "inferred: true",
        f"scan_days: {days}", f"source_claim_ids: {[item['id'] for item in claims]}", "---", "",
        "# Dream report", "", "This is inferred synthesis, not a replacement for source claims.",
        "", "## User Summary",
    ]
    report_lines.extend([f"- {item['content']} (source: {item['id']})" for item in preferences] or ["- No recent sourced preference is ready to summarize."])
    report_lines.extend(["", "## Project Digest"])
    report_lines.extend([f"- Repeated topic: {topic} ({topic_counts[topic]} sourced claims)" for topic in repeated] or ["- No repeated project topic found."])
    report_lines.extend(["", "## Procedure Candidate"])
    report_lines.extend([f"- {item['content']} (source: {item['id']})" for item in procedures] or ["- No repeated procedure candidate found."])
    report_lines.extend(["", "## Open Question"])
    report_lines.extend([f"- {row[1] or 'memory'}: {row[2] or row[0]}" for row in conflicts] or ["- No open confirmation item found."])
    dream_dir = root / "dream"
    dream_dir.mkdir(parents=True, exist_ok=True)
    report = dream_dir / f"dream-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.md"
    report.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    return {
        "status": "ok", "report": str(report), "inferred": True,
        "source_claim_ids": [item["id"] for item in claims], "repeated_topics": repeated,
        "open_questions": len(conflicts),
    }
