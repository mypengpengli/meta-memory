#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

from assemble_context import assemble_context, estimate_tokens
from classify_memory import classify
from extract_memory_units import sensitivity as detect_sensitivity
from ingest_memory import build_payload, load_payload as load_memory_payload, read_input
from ingest_raw_event import insert_raw_event
from write_memory import write_payload
from _common import DEFAULT_STORE_HELP, emit, ensure_store_ready, open_db, sha256_text, store_root
from config import get


def _retrieval_args(args: argparse.Namespace) -> argparse.Namespace:
    """Adapt the host runtime options to the shared retrieval service.

    Keeping this as a direct Python call is important: a user turn must not
    create subprocesses merely to read memory.
    """
    return argparse.Namespace(
        store=str(store_root(args.store)), query=None, query_file=None,
        top_k=args.top_k, candidate_pool=args.candidate_pool,
        expand_hops=args.expand_hops, session_id=args.session_id,
        workspace_id=args.workspace_id, profile_id=args.profile_id, agent_id=args.agent_id, active_subject_id=[], valid_at=None, no_chunks=bool(getattr(args, "no_chunks", False)),
        include_embeddings=bool(getattr(args, "include_embeddings", False)), embedding_model="external", rrf_k=60,
        include_dreams=bool(getattr(args, "include_dreams", True)),
        # Imported files are source evidence, not prompt memories.  Only the
        # explicit public search path opts into resource results.
        include_resources=False,
        subject_id=args.subject_id, subject_name=args.subject_name,
        domain=[], memory_kind=[], include_candidates=args.include_candidates,
        no_basics=args.no_basics,
    )


def _raw_search_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        store=str(store_root(args.store)), subject_id=args.subject_id,
        session_id=args.session_id, query=None, query_file=None, topic=[],
        domain=[], source_type=[], exclude_source_type=["resource", "agent-observation", "tool-result"], processed_state=["organized"], since=None,
        until=None, limit=args.raw_limit, full_content=False, profile_id=args.profile_id,
        workspace_id=args.workspace_id, agent_id=args.agent_id,
    )


def _auxiliary_context(*, sessions: dict[str, object] | None, procedures: list[dict[str, object]]) -> str:
    lines: list[str] = []
    session_rows = [
        item for item in list((sessions or {}).get("sessions", []))
        if str(item.get("source", "")).casefold() not in {"resource", "tool", "subagent", "agent-observation"}
    ]
    if session_rows:
        lines.extend(["## Relevant Past Sessions"])
        for item in session_rows[:3]:
            lines.append(f"- {item.get('title') or item.get('external_session_id') or 'session'}: {item.get('match_snippet', '')}")
    if procedures:
        lines.extend(["", "## Reusable Procedures"])
        for item in procedures[:4]:
            lines.append(f"- {item.get('task_class', 'procedure')}: {item.get('instruction_text', '')}")
    return "\n".join(lines).strip()


def _append_auxiliary_within_budget(context: str, auxiliary: str, budget: int) -> str:
    """Append session/procedure context inside the same hard prompt budget."""
    if not auxiliary.strip():
        return context
    closing = "\n</memory-context>"
    body, marker = (context.rsplit(closing, 1) + [""])[:2] if closing in context else (context, "")
    accepted: list[str] = []
    for line in auxiliary.splitlines():
        candidate = "\n".join(accepted + [line])
        if estimate_tokens(body + "\n" + candidate + (closing if marker is not None else "")) > budget:
            break
        accepted.append(line)
    if not accepted:
        return context
    return body.rstrip() + "\n\n" + "\n".join(accepted).rstrip() + (closing if closing in context else "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Host-facing runtime bridge for recording events, preparing context, and explicit remember actions."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record-event", help="Record an arbitrary raw event into the inbox")
    add_shared_record_args(record)
    record.add_argument("--allow-duplicate", action="store_true", help="Allow exact duplicate raw events")

    prepare = subparsers.add_parser("prepare-context", help="Record the current user turn, run heartbeat, and retrieve memory context")
    prepare.add_argument("--store", help=DEFAULT_STORE_HELP)
    prepare.add_argument("--subject-id", default="person-unknown", help="Primary subject id")
    prepare.add_argument("--subject-name", default="Unknown", help="Primary subject display name")
    prepare.add_argument("--session-id", default="", help="Session id")
    prepare.add_argument("--profile-id", default="default")
    prepare.add_argument("--workspace-id", default="default"); prepare.add_argument("--agent-id", default=""); prepare.add_argument("--visibility-scope", choices=["global", "workspace", "agent"], default="workspace"); prepare.add_argument("--shared-mode", action="store_true")
    prepare.add_argument("--hot-snapshot-policy", choices=["frozen", "refresh", "manual"], default="frozen")
    prepare.add_argument("--query", help="Current user query")
    prepare.add_argument("--query-file", help="Read the current user query from a UTF-8 text file")
    prepare.add_argument("--topic-hint", default="", help="Optional topic hint for the raw event")
    prepare.add_argument("--domain-hint", default="", help="Optional domain hint for the raw event")
    prepare.add_argument("--source-ref", default="", help="Optional raw event source reference")
    prepare.add_argument("--event-time", default="", help="Optional ISO-like event time")
    prepare.add_argument("--skip-record-query", action="store_true", help="Do not append the current query to raw_events")
    prepare.add_argument("--allow-duplicate", action="store_true", help="Allow exact duplicate raw events when recording the query")
    prepare.add_argument("--skip-heartbeat", action="store_true", help="Do not run heartbeat before retrieval")
    prepare.add_argument("--heartbeat-policy", choices=["conservative", "balanced", "aggressive"], default="conservative")
    prepare.add_argument("--heartbeat-interval-minutes", type=int, default=30)
    prepare.add_argument("--heartbeat-min-pending", type=int, default=3)
    prepare.add_argument("--heartbeat-max-events", type=int, default=20)
    prepare.add_argument("--top-k", type=int, default=6, help="Maximum retrieved memories")
    prepare.add_argument("--candidate-pool", type=int, default=24, help="Maximum internally ranked retrieval candidates")
    prepare.add_argument("--expand-hops", type=int, default=1, help="Association expansion hops through related fields, 0-2")
    prepare.add_argument("--include-candidates", action="store_true", help="Allow candidate memories in retrieval")
    prepare.add_argument("--no-basics", action="store_true", help="Do not prioritize relevant profile/state memories")
    prepare.add_argument("--raw-limit", type=int, default=3, help="Maximum raw evidence snippets to include")
    prepare.add_argument("--skip-raw-evidence", action="store_true", help="Do not search raw events for evidence snippets")
    prepare.add_argument("--context-token-budget", type=int, default=1800, help="Maximum estimated tokens in context_markdown")
    prepare.add_argument("--context-out-file", help="Write the rendered prompt context to a UTF-8 text file")
    prepare.add_argument("--out-file", help="Write the full JSON result to a UTF-8 file")

    finalize = subparsers.add_parser("finalize-turn", help="Record the assistant reply and optionally organize the finished turn")
    finalize.add_argument("--store", help=DEFAULT_STORE_HELP)
    finalize.add_argument("--subject-id", default="person-unknown", help="Primary subject id")
    finalize.add_argument("--subject-name", default="Unknown", help="Primary subject display name")
    finalize.add_argument("--session-id", default="", help="Session id")
    finalize.add_argument("--profile-id", default="default")
    finalize.add_argument("--workspace-id", default="default"); finalize.add_argument("--agent-id", default=""); finalize.add_argument("--visibility-scope", choices=["global", "workspace", "agent"], default="workspace"); finalize.add_argument("--shared-mode", action="store_true")
    finalize.add_argument("--reply", help="Assistant reply text")
    finalize.add_argument("--reply-file", help="Read the assistant reply from a UTF-8 text file")
    finalize.add_argument("--topic-hint", default="", help="Optional topic hint")
    finalize.add_argument("--domain-hint", default="", help="Optional domain hint")
    finalize.add_argument("--source-ref", default="", help="Optional source reference")
    finalize.add_argument("--event-time", default="", help="Optional ISO-like event time")
    finalize.add_argument("--allow-duplicate", action="store_true", help="Allow exact duplicate raw events")
    finalize.add_argument("--skip-record-reply", action="store_true", help="Do not record the assistant reply before heartbeat")
    finalize.add_argument("--skip-heartbeat", action="store_true", help="Do not run heartbeat after recording the reply")
    finalize.add_argument("--heartbeat-policy", choices=["conservative", "balanced", "aggressive"], default="conservative")
    finalize.add_argument("--heartbeat-interval-minutes", type=int, default=30)
    finalize.add_argument("--heartbeat-min-pending", type=int, default=2)
    finalize.add_argument("--heartbeat-max-events", type=int, default=20)
    finalize.add_argument("--capture-artifact", action="store_true", help="Write the reply into a structured memory page in a controlled way")
    finalize.add_argument("--artifact-title", help="Optional title for the captured reply artifact")
    finalize.add_argument("--artifact-title-file", help="Read the artifact title from a UTF-8 file")
    finalize.add_argument(
        "--artifact-kind",
        choices=["profile", "state", "event", "relationship", "goal", "domain", "session", "candidate"],
        help="Override the classifier result for the captured reply artifact",
    )
    finalize.add_argument(
        "--artifact-use-underlying-kind",
        action="store_true",
        help="If the reply classifies to session/candidate, promote it to the suggested long-term kind",
    )
    finalize.add_argument("--artifact-domain", help="Override the artifact domain")
    finalize.add_argument("--artifact-topic", help="Override the artifact topic")
    finalize.add_argument("--artifact-tag", action="append", default=[], help="Additional tag for the captured artifact; may be repeated")
    finalize.add_argument("--out-file", help="Write the full JSON result to a UTF-8 file")

    remember = subparsers.add_parser("remember", help="Explicitly write a memory while also recording its raw source")
    remember.add_argument("--store", help=DEFAULT_STORE_HELP)
    remember.add_argument("--subject-id", default="person-unknown", help="Primary subject id")
    remember.add_argument("--subject-name", default="Unknown", help="Primary subject display name")
    remember.add_argument("--session-id", default="", help="Session id")
    remember.add_argument("--profile-id", default="default"); remember.add_argument("--workspace-id", default="default"); remember.add_argument("--agent-id", default=""); remember.add_argument("--visibility-scope", choices=["global", "workspace", "agent"], default="workspace"); remember.add_argument("--shared-mode", action="store_true")
    remember.add_argument("--title", help="Memory title")
    remember.add_argument("--title-file", help="Read the memory title from a UTF-8 text file")
    remember.add_argument("--content", help="Memory content")
    remember.add_argument("--content-file", help="Read content from a UTF-8 text file")
    remember.add_argument("--payload-file", help="Read title/content/metadata from a UTF-8 JSON file")
    remember.add_argument(
        "--force-kind",
        choices=["profile", "state", "event", "relationship", "goal", "domain", "session", "candidate"],
        help="Override the classifier result",
    )
    remember.add_argument(
        "--use-underlying-kind",
        action="store_true",
        help="If the classifier recommends session/candidate, write to the suggested long-term kind instead",
    )
    remember.add_argument("--domain", help="Override domain")
    remember.add_argument("--topic", help="Override topic")
    remember.add_argument("--source", help="Override source")
    remember.add_argument("--start-at", help="Override start time")
    remember.add_argument("--end-at", help="Override end time")
    remember.add_argument("--confidence", type=float, help="Override confidence")
    remember.add_argument("--importance", type=float, help="Override importance score from 0.0 to 1.0")
    remember.add_argument("--status", help="Override status")
    remember.add_argument("--tag", action="append", default=[], help="Additional tag; may be repeated")
    remember.add_argument("--related-person", action="append", default=[], help="Related person; may be repeated")
    remember.add_argument("--related-event", action="append", default=[], help="Related event; may be repeated")
    remember.add_argument("--related-topic", action="append", default=[], help="Related topic/entity; may be repeated")
    remember.add_argument("--related-source", action="append", default=[], help="Related source; may be repeated")
    remember.add_argument("--slug", help="Override slug")
    remember.add_argument("--mode", choices=["create", "replace", "append"], default="create")
    remember.add_argument("--topic-hint", default="", help="Optional topic hint for the raw event")
    remember.add_argument("--domain-hint", default="", help="Optional domain hint for the raw event")
    remember.add_argument("--source-ref", default="", help="Optional raw event source reference")
    remember.add_argument("--event-time", default="", help="Optional ISO-like event time")
    remember.add_argument("--skip-raw-record", action="store_true", help="Do not append a raw event before writing the memory")
    remember.add_argument("--allow-duplicate", action="store_true", help="Allow exact duplicate raw events")
    remember.add_argument("--skip-index", action="store_true", help="Do not reindex/rescore after writing")
    remember.add_argument("--out-file", help="Write the full JSON result to a UTF-8 file")

    session_flush = subparsers.add_parser("session-flush", help="Build a session card and queue restricted review")
    session_flush.add_argument("--store", help=DEFAULT_STORE_HELP); session_flush.add_argument("--subject-id", required=True); session_flush.add_argument("--session-id", default="")
    session_search = subparsers.add_parser("session-search", help="Discover original archived sessions")
    session_search.add_argument("--store", help=DEFAULT_STORE_HELP); session_search.add_argument("--subject-id", required=True); session_search.add_argument("--profile-id", default="default"); session_search.add_argument("--workspace-id", default="default"); session_search.add_argument("--agent-id", default=""); session_search.add_argument("--query", required=True)
    session_scroll = subparsers.add_parser("session-scroll", help="Scroll original messages around an anchor")
    session_scroll.add_argument("--store", help=DEFAULT_STORE_HELP); session_scroll.add_argument("--session-id", required=True); session_scroll.add_argument("--subject-id", required=True); session_scroll.add_argument("--profile-id", default="default"); session_scroll.add_argument("--workspace-id", default="default"); session_scroll.add_argument("--agent-id", default=""); session_scroll.add_argument("--around-message-id", type=int, required=True); session_scroll.add_argument("--window", type=int, default=6)
    extract = subparsers.add_parser("extract-units", help="Extract atomic units from session cards")
    extract.add_argument("--store", help=DEFAULT_STORE_HELP); extract.add_argument("--subject-id"); extract.add_argument("--limit", type=int, default=20)
    dream = subparsers.add_parser("dream", help="Run deferred extraction and semantic consolidation")
    dream.add_argument("--store", help=DEFAULT_STORE_HELP); dream.add_argument("--subject-id", required=True); dream.add_argument("--policy", default="conservative"); dream.add_argument("--apply", action="store_true")
    review_run = subparsers.add_parser("review-run", help="Process durable background review jobs")
    review_run.add_argument("--store", help=DEFAULT_STORE_HELP); review_run.add_argument("--max-jobs", type=int, default=10); review_run.add_argument("--apply-low-risk", action="store_true")
    review_status = subparsers.add_parser("review-status", help="Show durable background review jobs")
    review_status.add_argument("--store", help=DEFAULT_STORE_HELP)
    for name, help_text in [("proposals-list", "List pending memory proposals"), ("proposals-list", "List pending memory proposals")]:
        if name not in subparsers.choices:
            item = subparsers.add_parser(name, help=help_text); item.add_argument("--store", help=DEFAULT_STORE_HELP)
    proposal_show = subparsers.add_parser("proposal-show", help="Show a memory proposal")
    proposal_show.add_argument("--store", help=DEFAULT_STORE_HELP); proposal_show.add_argument("--id", required=True)
    proposal_diff = subparsers.add_parser("proposal-diff", help="Show a memory proposal diff")
    proposal_diff.add_argument("--store", help=DEFAULT_STORE_HELP); proposal_diff.add_argument("--id", required=True)
    proposal_approve = subparsers.add_parser("proposal-approve", help="Approve exactly one staged memory proposal")
    proposal_approve.add_argument("--store", help=DEFAULT_STORE_HELP); proposal_approve.add_argument("--id"); proposal_approve.add_argument("--all-low-risk", action="store_true")
    proposal_reject = subparsers.add_parser("proposal-reject", help="Reject a staged memory proposal")
    proposal_reject.add_argument("--store", help=DEFAULT_STORE_HELP); proposal_reject.add_argument("--id", required=True); proposal_reject.add_argument("--note", default="")
    for command, feedback in [("mark-used", "used"), ("mark-helpful", "helpful"), ("mark-unhelpful", "unhelpful"), ("mark-outdated", "outdated"), ("mark-incorrect", "incorrect")]:
        marker = subparsers.add_parser(command, help=f"Record {feedback} memory feedback")
        marker.add_argument("--store", help=DEFAULT_STORE_HELP); marker.add_argument("--claim-id", required=True); marker.add_argument("--note", default="")
    for command, help_text in [("hot-memory-build", "Compile bounded hot-memory files"), ("hot-memory-show", "Show the frozen hot-memory projection"), ("hot-memory-status", "Show hot-memory snapshot metadata")]:
        hot = subparsers.add_parser(command, help=help_text); hot.add_argument("--store", help=DEFAULT_STORE_HELP); hot.add_argument("--subject-id", required=True); hot.add_argument("--profile-id", default="default"); hot.add_argument("--workspace-id", default="default")
    skills_pending = subparsers.add_parser("skills-pending", help="List skill changes awaiting approval")
    skills_pending.add_argument("--store", help=DEFAULT_STORE_HELP)
    skill_diff = subparsers.add_parser("skill-diff", help="Show a staged skill diff")
    skill_diff.add_argument("--store", help=DEFAULT_STORE_HELP); skill_diff.add_argument("--id", required=True)
    skill_approve = subparsers.add_parser("skill-approve", help="Approve and write a staged skill change")
    skill_approve.add_argument("--store", help=DEFAULT_STORE_HELP); skill_approve.add_argument("--id", required=True)
    skill_reject = subparsers.add_parser("skill-reject", help="Reject a staged skill change")
    skill_reject.add_argument("--store", help=DEFAULT_STORE_HELP); skill_reject.add_argument("--id", required=True); skill_reject.add_argument("--note", default="")
    explain = subparsers.add_parser("explain-recall", help="Explain deterministic recall candidates")
    explain.add_argument("--store", help=DEFAULT_STORE_HELP); explain.add_argument("--subject-id", required=True); explain.add_argument("--query", required=True)
    provider_status = subparsers.add_parser("provider-status", help="Show provider and runtime health")
    provider_status.add_argument("--store", help=DEFAULT_STORE_HELP)
    worker_status = subparsers.add_parser("worker-status", help="Show durable worker queue state")
    worker_status.add_argument("--store", help=DEFAULT_STORE_HELP)
    maintenance = subparsers.add_parser("maintenance", help="Run the complete 2.1 maintenance sequence")
    maintenance.add_argument("--store", help=DEFAULT_STORE_HELP); maintenance.add_argument("--policy", default="conservative"); maintenance.add_argument("--max-review-jobs", type=int, default=20); maintenance.add_argument("--shadow-high-risk", action="store_true")
    doctor = subparsers.add_parser("doctor", help="Report store health without changing data")
    doctor.add_argument("--store", help=DEFAULT_STORE_HELP)

    return parser.parse_args()


def add_shared_record_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--subject-id", default="person-unknown", help="Primary subject id")
    parser.add_argument("--subject-name", default="Unknown", help="Primary subject display name")
    parser.add_argument("--session-id", default="", help="Session id")
    parser.add_argument("--profile-id", default="default"); parser.add_argument("--workspace-id", default="default"); parser.add_argument("--agent-id", default=""); parser.add_argument("--visibility-scope", choices=["global", "workspace", "agent"], default="workspace"); parser.add_argument("--shared-mode", action="store_true")
    parser.add_argument("--source-type", default="conversation", help="Raw event source type")
    parser.add_argument("--source-ref", default="", help="Optional raw event source reference")
    parser.add_argument("--topic-hint", default="", help="Optional topic hint")
    parser.add_argument("--domain-hint", default="", help="Optional domain hint")
    parser.add_argument("--event-time", default="", help="Optional ISO-like event time")
    parser.add_argument("--content", help="Inline raw content")
    parser.add_argument("--content-file", help="Read raw content from a UTF-8 text file")
    parser.add_argument("--payload-file", help="Read a UTF-8 JSON payload file")


def read_text_arg(value: str | None, path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8-sig").strip()
    return (value or "").strip()


def write_json_file(path: str | None, payload: dict[str, object]) -> None:
    if not path:
        return
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_json_script(script_name: str, *args: str) -> dict[str, object]:
    base = Path(__file__).resolve().parent
    result = subprocess.run(
        [sys.executable, str(base / script_name), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def format_memory_context(retrieved: dict[str, object], raw_evidence: dict[str, object] | None) -> str:
    evidence_rows = list((raw_evidence or {}).get("results", []))
    evidence_rows = [item for item in evidence_rows if str(item.get("source_type", "")).casefold() != "resource"]
    positive_evidence = [item for item in evidence_rows if float(item.get("score", 0.0) or 0.0) > 0.0]
    if positive_evidence:
        evidence_rows = positive_evidence

    selected = [
        item
        for item in list(retrieved.get("selected", []))
        if str(item.get("memory_kind", "")).casefold() != "resource"
        and str(item.get("prompt_eligible", "true")).strip().casefold() not in {"0", "false", "no", "off"}
    ]
    relevant_selected = [item for item in selected if float(item.get("query_score", 0.0) or 0.0) > 0.0]

    lines = [
        "# Memory Context",
        "",
        "Use these memories only when they are relevant to the current turn.",
        "Prefer current facts and direct answers over irrelevant historical detail.",
        "",
        "## Retrieved Memories",
    ]

    display_selected = relevant_selected

    if not display_selected:
        lines.append("- No relevant structured memories were found.")
    else:
        for item in display_selected:
            summary = str(item.get("summary", "")).strip() or str(item.get("topic", "")).strip() or str(item.get("title", "")).strip()
            verification = str(item.get("verification_state", "")).casefold().replace("-", "_")
            provenance = " [Agent-observed]" if verification == "agent_observed" else ""
            lines.append(
                f"- [{item.get('memory_kind', 'note')}] {item.get('title', '')}{provenance} | {item.get('domain', '')} / {item.get('topic', '')} | {summary}"
            )

    lines.extend(["", "## Raw Evidence"])
    if not evidence_rows:
        lines.append("- No additional raw evidence was selected.")
    else:
        for item in evidence_rows:
            source_type = str(item.get("source_type", "") or "")
            observed = source_type.casefold() in {"agent-observation", "tool-result", "subagent"}
            provenance = "Agent-observed" if observed else "Recorded evidence"
            reference = str(item.get("source_ref", "") or "")
            lines.append(
                f"- {item.get('effective_time', '')} | {provenance} ({source_type or 'unknown'})"
                f" | {item.get('domain_hint', '')} / {item.get('topic_hint', '')}"
                f" | {reference if reference else 'no-ref'} | {item.get('snippet', '')}"
            )

    return "\n".join(lines).strip() + "\n"


def link_memory_source(conn, memory_path: str, raw_event_id: int, link_role: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO memory_sources(memory_path, raw_event_id, link_role)
        VALUES(?, ?, ?)
        """,
        (memory_path, raw_event_id, link_role),
    )


def set_raw_event_state(root: Path, raw_event_id: int, state: str, note: dict[str, object] | None = None) -> None:
    conn = open_db(root)
    conn.execute(
        """
        UPDATE raw_events
        SET
            processed_state = ?,
            note = COALESCE(?, note)
        WHERE id = ?
        """,
        (
            state,
            json.dumps(note, ensure_ascii=False) if note is not None else None,
            raw_event_id,
        ),
    )
    conn.commit()
    conn.close()


def mark_raw_event_organized(
    root: Path,
    raw_event_id: int,
    *,
    classifier_kind: str,
    classifier_domain: str,
    target_memory_kind: str,
    target_memory_path: str,
    note: dict[str, object],
    link_role: str,
) -> None:
    conn = open_db(root)
    conn.execute(
        """
        UPDATE raw_events
        SET
            processed_state = 'organized',
            processed_at = CURRENT_TIMESTAMP,
            classifier_kind = ?,
            classifier_domain = ?,
            target_memory_kind = ?,
            target_memory_path = ?,
            note = ?
        WHERE id = ?
        """,
        (
            classifier_kind,
            classifier_domain,
            target_memory_kind,
            target_memory_path,
            json.dumps(note, ensure_ascii=False),
            raw_event_id,
        ),
    )
    link_memory_source(conn, target_memory_path, raw_event_id, link_role)
    conn.commit()
    conn.close()


def prepare_context(args: argparse.Namespace) -> dict[str, object]:
    root = store_root(args.store)
    bootstrap = ensure_store_ready(root)
    query = read_text_arg(args.query, args.query_file)
    if not query:
        raise SystemExit("Query is required via --query or --query-file.")

    from build_hot_memory import freeze_hot_snapshot
    from query_router import route_query
    from session_archive import ensure_session
    from retrieve_memories import retrieve
    from search_raw_events import search_events
    from session_search import discovery
    from procedural_learning import retrieve_procedures
    route = route_query(query)
    depth = str(getattr(args, "search_depth", "auto") or "auto").casefold()
    if depth not in {"light", "normal", "deep", "auto"}:
        depth = "auto"
    if depth == "light":
        route.update(needs_deep_memory=True, needs_raw_evidence=False, needs_session_search=False, needs_procedure=False, needs_dream_digest=False)
    elif depth == "normal":
        route.update(needs_deep_memory=True, needs_raw_evidence=False, needs_session_search=False, needs_procedure=False)
    elif depth == "deep":
        route.update(needs_deep_memory=True, needs_raw_evidence=True, needs_session_search=True, needs_procedure=True, needs_dream_digest=True)
    route["search_depth"] = depth
    internal_session_id = ensure_session(root, subject_id=args.subject_id, session_id=args.session_id, profile_id=args.profile_id, workspace_id=args.workspace_id, origin_agent_id=args.agent_id, shared_mode=args.shared_mode)
    hot_snapshot = freeze_hot_snapshot(root, internal_session_id=internal_session_id, subject_id=args.subject_id, profile_id=args.profile_id, workspace_id=args.workspace_id, agent_id=args.agent_id, policy=args.hot_snapshot_policy)
    hot_context, hot_snapshot_hash = str(hot_snapshot["content"]), str(hot_snapshot["content_hash"])
    retrieval_args = _retrieval_args(args)
    retrieval_args.no_chunks = bool(getattr(args, "no_chunks", False) or depth == "light")
    retrieval_args.include_dreams = bool(route.get("needs_dream_digest", True))
    retrieved = retrieve(retrieval_args, query=query) if bool(route.get("needs_deep_memory", True)) else {"status": "ok", "selected": [], "query": query}

    raw_evidence = None
    if not args.skip_raw_evidence and args.raw_limit > 0 and bool(route.get("needs_raw_evidence")):
        raw_evidence = search_events(_raw_search_args(args), query=query)

    session_evidence = discovery(
        root, subject_id=args.subject_id, query=query, limit=3,
        workspace_id=args.workspace_id, profile_id=args.profile_id,
        agent_id=args.agent_id, exclude_session_id=args.session_id,
    ) if route.get("needs_session_search") else None
    procedures = retrieve_procedures(root, subject_id=args.subject_id, query=query, profile_id=args.profile_id, workspace_id=args.workspace_id, agent_id=args.agent_id) if route.get("needs_procedure") else []

    recorded = None
    if not args.skip_record_query:
        recorded = insert_raw_event(
            root,
            subject_id=args.subject_id,
            subject_name=args.subject_name,
            session_id=args.session_id,
            source_type="conversation-user",
            source_ref=args.source_ref,
            topic_hint=args.topic_hint,
            domain_hint=args.domain_hint,
            event_time=args.event_time,
            content=query,
            allow_duplicate=args.allow_duplicate,
            profile_id=args.profile_id,
            workspace_id=args.workspace_id,
            origin_agent_id=args.agent_id,
            visibility_scope=args.visibility_scope,
            shared_mode=args.shared_mode,
        )

    context = assemble_context(retrieved, raw_evidence, token_budget=args.context_token_budget)
    auxiliary = _auxiliary_context(sessions=session_evidence, procedures=procedures)
    context = _append_auxiliary_within_budget(context, auxiliary, args.context_token_budget)
    if args.context_out_file:
        Path(args.context_out_file).write_text(context, encoding="utf-8")

    result = {
        "status": "ok",
        "command": "prepare-context",
        "store_bootstrap": bootstrap,
        "query": query,
        "query_route": route,
        "hot_memory_snapshot_hash": hot_snapshot_hash,
        "hot_memory_snapshot_uid": hot_snapshot["snapshot_uid"],
        "internal_session_id": internal_session_id,
        "static_hot_context": hot_context,
        "recorded_raw_event": recorded,
        "heartbeat": {"status": "deferred", "reason": "review is scheduled after the assistant reply"},
        "retrieved": retrieved,
        "raw_evidence": raw_evidence,
        "session_evidence": session_evidence,
        "procedures": procedures,
        "context_markdown": context,
    }
    write_json_file(args.out_file, result)
    return result


def capture_reply_artifact(
    root: Path,
    args: argparse.Namespace,
    reply: str,
    raw_record: dict[str, object] | None,
) -> dict[str, object] | None:
    if not args.capture_artifact or not reply.strip():
        return None

    title = read_text_arg(args.artifact_title, args.artifact_title_file) or reply.splitlines()[0][:60].strip() or "Reply Artifact"
    payload: dict[str, object] = {}
    artifact_args = argparse.Namespace(
        subject_id=args.subject_id,
        subject_name=args.subject_name,
        force_kind=args.artifact_kind,
        use_underlying_kind=args.artifact_use_underlying_kind,
        domain=args.artifact_domain,
        topic=args.artifact_topic,
        source=None,
        start_at=None,
        end_at=None,
        confidence=None,
        importance=None,
        status=None,
        tag=args.artifact_tag or [],
        related_person=[],
        related_event=[],
        related_topic=[],
        related_source=[],
        slug=None,
        mode="create",
    )
    classification = classify(title, reply, args.subject_id, args.subject_name)
    final_payload = build_payload(classification, payload, artifact_args, title, reply)
    final_payload["subject_id"] = args.subject_id
    final_payload["subject_name"] = args.subject_name

    if raw_record and raw_record.get("inserted"):
        raw_source = f"raw_event:{raw_record['raw_event_id']}"
        final_payload["source"] = raw_source
        related_sources = list(final_payload.get("related_sources", []))
        if raw_source not in related_sources:
            related_sources.append(raw_source)
        final_payload["related_sources"] = related_sources

    if not raw_record or not raw_record.get("inserted"):
        return {"status": "skipped", "reason": "artifact_requires_raw_source"}
    action = {
        "plan_id": str(uuid.uuid4()),
        "action": "CREATE",
        "subject_id": args.subject_id,
        "subject_name": args.subject_name,
        "source_event_ids": [int(raw_record["raw_event_id"])],
        "memory_kind": "candidate",
        "topic": str(final_payload["topic"]),
        "title": title,
        "content": reply,
        "confidence": float(final_payload["confidence"]),
        "uncertainty": 0.5,
        "importance": float(final_payload["importance"]),
        "sensitivity": detect_sensitivity(reply),
        "verification_state": "unverified",
        "prompt_eligible": False,
        "profile_id": args.profile_id,
        "workspace_id": args.workspace_id,
        "origin_agent_id": args.agent_id,
        "visibility_scope": args.visibility_scope,
        "owner_agent_id": args.agent_id if args.visibility_scope == "agent" else "",
    }
    from apply_memory_plan import apply_plan

    applied = apply_plan(root, {"schema_version": 2, "subject_id": args.subject_id, "policy": "conservative", "actions": [action]})
    if applied["status"] != "ok" or applied["results"][0]["status"] != "applied":
        return {"status": "pending_review", "classification": classification, "plan": action, "applied": applied}
    written = applied["results"][0]
    mark_raw_event_organized(
        root,
        int(raw_record["raw_event_id"]),
        classifier_kind=str(classification["recommended_kind"]),
        classifier_domain=str(classification["recommended_domain"]),
        target_memory_kind=str(final_payload["kind"]),
        target_memory_path=str(written.get("path", "")),
        note={"origin": "reply-artifact-v2", "plan_id": action["plan_id"], "final_kind": final_payload["kind"]},
        link_role="reply-artifact",
    )

    return {
        "status": "ok",
        "classification": classification,
        "plan": action,
        "written": written,
        "applied": applied,
    }


def remember_memory(args: argparse.Namespace) -> dict[str, object]:
    root = store_root(args.store)
    bootstrap = ensure_store_ready(root)
    if args.skip_raw_record:
        raise SystemExit("V2 remember requires a raw source record so it can deduplicate and validate provenance.")
    payload = load_memory_payload(args.payload_file)
    title, content = read_input(args, payload)
    subject_id = str(payload.get("subject_id", args.subject_id))
    subject_name = str(payload.get("subject_name", args.subject_name))

    source_kind = str(getattr(args, "source_kind", "user") or "user").casefold()
    source_types = {
        "user": "explicit-memory",
        "agent-observation": "agent-observation",
        "tool-result": "tool-result",
        "resource": "resource",
    }
    if source_kind not in source_types:
        raise ValueError("source_kind must be user, agent-observation, tool-result, or resource.")
    if source_kind == "agent-observation" and not str(args.source_ref or "").strip():
        raise ValueError("Agent observations require --source-ref evidence such as a commit, file, or command result.")
    if source_kind in {"agent-observation", "tool-result", "resource"} and (
        str(args.visibility_scope or "").casefold() != "workspace"
        or str(args.workspace_id or "").casefold() == "global"
    ):
        raise ValueError("Agent observations, tool results, and resources must use a project workspace scope.")

    # A deliberate remember is new evidence even when the wording repeats.
    # In contrast, a tool host call and a resource file have natural stable
    # identities, so retrying their ingestion must remain idempotent.
    if source_kind == "user":
        event_key = f"remember:{uuid.uuid4()}"
    elif source_kind == "tool-result":
        event_key = f"tool:{str(args.source_ref or '').strip() or uuid.uuid4()}"
    elif source_kind == "resource":
        event_key = f"resource:{str(args.source_ref or '').strip() or sha256_text(content)}"
    else:
        event_key = f"agent-observation:{str(args.source_ref or '').strip()}"

    raw_record = insert_raw_event(
        root,
        subject_id=subject_id,
        subject_name=subject_name,
        session_id=args.session_id,
        source_type=source_types[source_kind],
        source_ref=args.source_ref,
        topic_hint=args.topic_hint or args.topic or "",
        domain_hint=args.domain_hint or args.domain or "",
        event_time=args.event_time,
        content=content,
        allow_duplicate=args.allow_duplicate,
        profile_id=args.profile_id,
        workspace_id=args.workspace_id,
        origin_agent_id=args.agent_id,
        visibility_scope=args.visibility_scope,
        event_uid=event_key,
        idempotency_key=event_key,
        shared_mode=args.shared_mode,
    )
    if not raw_record.get("inserted"):
        return {"status": "ok", "command": "remember", "store_bootstrap": bootstrap, "raw_event": raw_record, "deduplicated": True}
    set_raw_event_state(root, int(raw_record["raw_event_id"]), "in_progress", {"origin": "explicit-remember", "stage": "planning"})
    classification = classify(title, content, subject_id, subject_name)
    recommended = str(classification["recommended_kind"])
    kind = args.force_kind or (str(classification["underlying_long_term_kind"]) if args.use_underlying_kind or recommended in {"candidate", "session"} else recommended)
    # Tool-backed observations describe project state/work, never a user's
    # personal profile even when a short sentence confuses the classifier.
    if source_kind in {"agent-observation", "tool-result"} and kind == "profile":
        kind = "state"
    confidence = 0.80 if source_kind in {"agent-observation", "tool-result"} else max(float(classification["suggested_payload"]["confidence"]), 0.95)
    # Explicit remember uses the same semantic consolidation path as the
    # background pipeline. Hash equality is only the first fast-path; same
    # predicate/object and state changes are handled as corroborate/refine/
    # reviewable temporal actions as well.
    from consolidate_memories import build_plan_for_unit
    from extract_memory_units import structured_fields
    fields = structured_fields(content, args.topic or str(classification["suggested_payload"]["topic"]), args.domain or "")
    unit = {
        "id": None, "subject_id": subject_id, "source_event_ids": [int(raw_record["raw_event_id"])], "topic": args.topic or str(fields["topic"]),
        "domain": args.domain or str(fields["domain"]), "content": content, "content_hash": sha256_text(content), "confidence": confidence,
        "uncertainty": 0.0, "importance": args.importance if args.importance is not None else float(classification["suggested_payload"]["importance"]),
        "durability": float(fields["durability"]), "sensitivity": detect_sensitivity(content), "unit_kind": kind,
        "predicate": str(fields["predicate"]), "subject_text": str(fields["subject_text"]), "object_text": str(fields["object_text"]), "qualifiers": {},
        "valid_from": args.start_at or args.event_time or "", "valid_to": args.end_at or "", "observed_at": "", "entities": [],
        "profile_id": args.profile_id, "workspace_id": args.workspace_id, "origin_agent_id": args.agent_id,
        "visibility_scope": args.visibility_scope, "owner_agent_id": args.agent_id if args.visibility_scope == "agent" else "",
        "source_type": source_types[source_kind],
    }
    action = build_plan_for_unit(root, unit, policy="balanced")
    action.update({"subject_name": subject_name, "title": title, "origin": "explicit_remember", "profile_id": args.profile_id, "workspace_id": args.workspace_id, "origin_agent_id": args.agent_id, "visibility_scope": args.visibility_scope, "owner_agent_id": args.agent_id if args.visibility_scope == "agent" else "", "source_type": source_types[source_kind], "source_kind": source_kind, "explicit_user_action": source_kind == "user", "prompt_eligible": source_kind != "resource"})
    if action["action"] == "CREATE":
        verification = "agent_observed" if source_kind in {"agent-observation", "tool-result"} else "resource" if source_kind == "resource" else "verified" if kind not in {"candidate", "session"} else "unverified"
        final_kind = "state" if source_kind in {"agent-observation", "tool-result"} and kind in {"profile", "candidate", "session"} else kind
        action.update({"memory_kind": final_kind, "verification_state": verification})
    from apply_memory_plan import apply_plan

    applied = apply_plan(root, {"schema_version": 2, "subject_id": subject_id, "policy": "balanced", "actions": [action]}, skip_index=args.skip_index)
    if applied["status"] != "ok" or not applied["results"] or applied["results"][0]["status"] != "applied":
        set_raw_event_state(root, int(raw_record["raw_event_id"]), "pending", {"origin": "explicit-remember", "stage": "validation_or_apply_failed", "result": applied})
        return {"status": "invalid", "command": "remember", "store_bootstrap": bootstrap, "classification": classification, "raw_event": raw_record, "plan": action, "applied": applied}
    written = applied["results"][0]
    mark_raw_event_organized(
        root,
        int(raw_record["raw_event_id"]),
        classifier_kind=str(classification["recommended_kind"]),
        classifier_domain=str(classification["recommended_domain"]),
        target_memory_kind=kind,
        target_memory_path=str(written.get("path", "")),
        note={"origin": "explicit-remember-v2", "plan_id": action["plan_id"], "final_kind": kind},
        link_role="explicit-remember",
    )
    if not args.skip_index:
        from projection_outbox import process_projection_outbox
        projection = process_projection_outbox(root, limit=10)
    else:
        projection = {"status": "deferred", "reason": "skip_index"}

    result = {
        "status": "ok",
        "command": "remember",
        "store_bootstrap": bootstrap,
        "classification": classification,
        "plan": action,
        "raw_event": raw_record,
        "written": written,
        "applied": applied,
        "projection": projection,
    }
    write_json_file(args.out_file, result)
    return result


def record_event(args: argparse.Namespace) -> dict[str, object]:
    payload = load_memory_payload(args.payload_file)
    content = read_text_arg(args.content, args.content_file) or str(payload.get("content", "")).strip()
    if not content:
        raise SystemExit("Content is required via --content, --content-file, or --payload-file.")
    root = store_root(args.store)
    bootstrap = ensure_store_ready(root)
    result = insert_raw_event(
        root,
        subject_id=str(payload.get("subject_id", args.subject_id)),
        subject_name=str(payload.get("subject_name", args.subject_name)),
        session_id=str(payload.get("session_id", args.session_id)),
        source_type=str(payload.get("source_type", args.source_type)),
        source_ref=str(payload.get("source_ref", args.source_ref)),
        topic_hint=str(payload.get("topic_hint", args.topic_hint)),
        domain_hint=str(payload.get("domain_hint", args.domain_hint)),
        event_time=str(payload.get("event_time", args.event_time)),
        content=content,
        allow_duplicate=bool(payload.get("allow_duplicate", False) or args.allow_duplicate),
        profile_id=str(payload.get("profile_id", args.profile_id)),
        workspace_id=str(payload.get("workspace_id", args.workspace_id)),
        origin_agent_id=str(payload.get("agent_id", args.agent_id)),
        visibility_scope=str(payload.get("visibility_scope", args.visibility_scope)),
        shared_mode=bool(payload.get("shared_mode", args.shared_mode)),
    )
    return {"status": "ok", "command": "record-event", "store_bootstrap": bootstrap, "result": result}


def finalize_turn(args: argparse.Namespace) -> dict[str, object]:
    root = store_root(args.store)
    bootstrap = ensure_store_ready(root)
    reply = read_text_arg(args.reply, args.reply_file)
    if not reply and not args.skip_record_reply:
        raise SystemExit("Reply is required via --reply or --reply-file unless --skip-record-reply is set.")

    recorded = None
    if not args.skip_record_reply:
        recorded = insert_raw_event(
            root,
            subject_id=args.subject_id,
            subject_name=args.subject_name,
            session_id=args.session_id,
            source_type="conversation-assistant",
            source_ref=args.source_ref,
            topic_hint=args.topic_hint,
            domain_hint=args.domain_hint,
            event_time=args.event_time,
            content=reply,
            allow_duplicate=args.allow_duplicate,
            profile_id=args.profile_id,
            workspace_id=args.workspace_id,
            origin_agent_id=args.agent_id,
            visibility_scope=args.visibility_scope,
            shared_mode=args.shared_mode,
        )

    artifact = capture_reply_artifact(root, args, reply, recorded)

    heartbeat = None
    if not args.skip_heartbeat:
        from background_review import enqueue_review
        conn = open_db(root)
        bounds = conn.execute(
            "SELECT MIN(id), MAX(id) FROM raw_events WHERE subject_id=? AND COALESCE(session_id, '')=? AND profile_id=? AND workspace_id=? AND processed_state IN ('pending', 'sessionized')",
            (args.subject_id, args.session_id, args.profile_id, args.workspace_id),
        ).fetchone()
        user_turns = conn.execute("SELECT COUNT(*) FROM raw_events WHERE subject_id=? AND COALESCE(session_id,'')=? AND profile_id=? AND workspace_id=? AND source_type='conversation-user'", (args.subject_id, args.session_id, args.profile_id, args.workspace_id)).fetchone()[0]
        conn.close()
        interval = max(1, int(get("review.every_n_user_turns")))
        if bounds and bounds[0] is not None and int(user_turns) % interval == 0:
            heartbeat = enqueue_review(
                root, subject_id=args.subject_id, session_id=args.session_id,
                event_start_id=int(bounds[0]), event_end_id=int(bounds[1]),
                trigger_type="turn_end", profile_id=args.profile_id, workspace_id=args.workspace_id, origin_agent_id=args.agent_id,
            )
        else:
            heartbeat = {"status": "not_scheduled", "reason": "review_interval_not_reached" if bounds and bounds[0] is not None else "no pending session events", "every_n_user_turns": interval}

    result = {
        "status": "ok",
        "command": "finalize-turn",
        "store_bootstrap": bootstrap,
        "recorded_raw_event": recorded,
        "artifact": artifact,
        "heartbeat": heartbeat,
    }
    write_json_file(args.out_file, result)
    return result


def main() -> None:
    args = parse_args()
    if args.command == "prepare-context":
        emit(prepare_context(args))
        return
    if args.command == "remember":
        emit(remember_memory(args))
        return
    if args.command == "record-event":
        emit(record_event(args))
        return
    if args.command == "finalize-turn":
        emit(finalize_turn(args))
        return
    root = store_root(getattr(args, "store", None))
    if args.command == "session-flush":
        from background_review import enqueue_review
        from build_session_card import build_cards
        cards = build_cards(root, subject_id=args.subject_id, session_id=args.session_id or None, force=True)
        event_ids = [event_id for card in cards["cards"] for event_id in card.get("source_event_ids", [])]
        emit({"status": "ok", "cards": cards, "review": enqueue_review(root, subject_id=args.subject_id, session_id=args.session_id, event_start_id=min(event_ids) if event_ids else 0, event_end_id=max(event_ids) if event_ids else 0, trigger_type="explicit_flush")})
        return
    if args.command == "session-search":
        from session_search import discovery
        emit(discovery(root, subject_id=args.subject_id, query=args.query, profile_id=args.profile_id, workspace_id=args.workspace_id, agent_id=args.agent_id)); return
    if args.command == "session-scroll":
        from session_search import scroll
        emit(scroll(root, session_id=args.session_id, around_message_id=args.around_message_id, window=args.window, subject_id=args.subject_id, profile_id=args.profile_id, workspace_id=args.workspace_id, agent_id=args.agent_id)); return
    if args.command == "extract-units":
        from extract_memory_units import extract_units
        emit(extract_units(root, subject_id=args.subject_id, limit=args.limit)); return
    if args.command == "dream":
        from consolidate_memories import build_plan
        from extract_memory_units import extract_units
        units = extract_units(root, subject_id=args.subject_id)
        plan = build_plan(root, args.subject_id, policy=args.policy)
        from apply_memory_plan import apply_plan
        emit({"status": "ok", "units": units, "plan": plan, "result": apply_plan(root, plan) if args.apply else {"status": "shadow", "actions": plan["actions"]}}); return
    if args.command == "review-run":
        from background_review import run_pending
        emit(run_pending(root, max_jobs=args.max_jobs, apply_low_risk=args.apply_low_risk)); return
    if args.command == "review-status":
        conn = open_db(root); rows = conn.execute("SELECT job_uid, subject_id, session_id, trigger_type, status, attempt_count, last_error, created_at FROM review_jobs ORDER BY created_at DESC").fetchall(); conn.close()
        emit({"status": "ok", "jobs": [{"id": row[0], "subject_id": row[1], "session_id": row[2], "trigger": row[3], "status": row[4], "attempts": row[5], "error": row[6], "created_at": row[7]} for row in rows]}); return
    if args.command.startswith("proposal-") or args.command == "proposals-list":
        from proposal_manager import approve_memory_proposal, get_proposal, list_proposals, reject_proposal
        if args.command == "proposals-list": emit({"status": "ok", "proposals": list_proposals(root)}); return
        if args.command in {"proposal-show", "proposal-diff"}:
            proposal = get_proposal(root, args.id)
            emit({"status": "ok", "proposal": proposal if args.command == "proposal-show" else {"id": args.id, "diff": proposal["diff"] if proposal else ""}}); return
        if args.command == "proposal-reject": emit({"status": "ok", "rejected": reject_proposal(root, args.id, note=args.note)}); return
        if args.command == "proposal-approve":
            proposals = [get_proposal(root, args.id)] if args.id else [get_proposal(root, item["id"]) for item in list_proposals(root)]
            results = [approve_memory_proposal(root, proposal["id"]) for proposal in proposals if proposal and not (args.all_low_risk and str(proposal["action"]) in {"CORRECT", "SUPERSEDE"})]
            emit({"status": "ok", "results": results}); return
    if args.command.startswith("mark-"):
        from feedback_memory import record_feedback
        emit(record_feedback(root, claim_id=args.claim_id, feedback_type=args.command.removeprefix("mark-").replace("helpful", "helpful"), note=args.note)); return
    if args.command.startswith("hot-memory-"):
        from build_hot_memory import build_hot_memory, load_hot_memory
        if args.command == "hot-memory-build": emit(build_hot_memory(root, subject_id=args.subject_id, profile_id=args.profile_id, workspace_id=args.workspace_id, force=True)); return
        if args.command == "hot-memory-show":
            build_hot_memory(root, subject_id=args.subject_id, profile_id=args.profile_id, workspace_id=args.workspace_id); snapshot = load_hot_memory(root, subject_id=args.subject_id, profile_id=args.profile_id, workspace_id=args.workspace_id); emit({"status": "ok", **snapshot}); return
        snapshot = load_hot_memory(root, subject_id=args.subject_id, profile_id=args.profile_id, workspace_id=args.workspace_id); emit({"status": "ok", "snapshot": snapshot}); return
    if args.command.startswith("skill") or args.command == "skills-pending":
        from procedural_learning import approve_skill, skill_diff
        from proposal_manager import list_proposals, reject_proposal
        if args.command == "skills-pending": emit({"status": "ok", "proposals": list_proposals(root, kind="skill")}); return
        if args.command == "skill-diff": emit({"status": "ok", "diff": skill_diff(root, args.id)}); return
        if args.command == "skill-approve": emit(approve_skill(root, args.id)); return
        emit({"status": "ok", "rejected": reject_proposal(root, args.id, note=args.note, kind="skill")}); return
    if args.command == "explain-recall":
        from node_search import search_nodes
        from query_router import route_query
        emit({"status": "ok", "route": route_query(args.query), "candidates": search_nodes(root, args.subject_id, args.query, include_units=True)}); return
    if args.command == "provider-status":
        from doctor import doctor as run_doctor
        emit({"status": "ok", "builtin_provider": "meta-memory", "external_provider_limit": 1, "health": run_doctor(root)}); return
    if args.command == "worker-status":
        conn = open_db(root); rows = conn.execute("SELECT status, COUNT(*) FROM review_jobs GROUP BY status").fetchall(); conn.close(); emit({"status": "ok", "durable_jobs": {str(row[0]): int(row[1]) for row in rows}}); return
    if args.command == "maintenance":
        result = run_json_script("run_maintenance.py", "--store", str(root), "--max-projection-jobs", str(args.max_review_jobs)); emit(result); return
    if args.command == "doctor":
        from doctor import doctor as run_doctor
        emit(run_doctor(root)); return
    raise SystemExit(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
