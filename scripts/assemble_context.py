#!/usr/bin/env python3
"""Assemble bounded, source-aware prompt context from retrieval results only."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import get


def estimate_tokens(text: str) -> int:
    # A conservative dependency-free estimate suitable for a context guardrail.
    return max(1, (len(text) + 3) // 4)


def compact(value: str, limit: int = 460) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render_item(item: dict[str, object]) -> str:
    chunk = item.get("best_chunk") if isinstance(item.get("best_chunk"), dict) else {}
    excerpt = compact(str(chunk.get("content", "") or item.get("summary", "") or item.get("title", "")))
    line_range = ""
    if chunk and chunk.get("start_line"):
        line_range = f":{chunk['start_line']}-{chunk.get('end_line', chunk['start_line'])}"
    path = str(item.get("path", ""))
    source = f"{path}{line_range}" if path else "structured memory"
    validity = "current" if not item.get("end_at") else f"until {item['end_at']}"
    return "\n".join(
        [
            f"### {item.get('title', 'Untitled memory')}",
            f"- Type: {item.get('memory_kind', 'note')} | Status: {item.get('status', 'active')} | Validity: {validity}",
            f"- Source: {source}",
            f"- Recall: {', '.join(str(value) for value in item.get('reasons', [])[:3]) or 'ranked match'}",
            f"- Memory: {excerpt}",
        ]
    )


def assemble_context(retrieved: dict[str, object] | None, raw_evidence: dict[str, object] | None = None, *, token_budget: int | None = None) -> str:
    budget = token_budget or int(get("retrieval.context_token_budget"))
    selected = list((retrieved or {}).get("selected", []))
    selected = [item for item in selected if float(item.get("query_score", 0.0) or 0.0) > 0.0]
    sections: dict[str, list[dict[str, object]]] = {"Current Relevant Memory": [], "Current Project State": [], "Candidates": []}
    for item in selected:
        kind = str(item.get("memory_kind", ""))
        if kind in {"state", "goal", "session"}:
            sections["Current Project State"].append(item)
        elif kind == "candidate":
            sections["Candidates"].append(item)
        else:
            sections["Current Relevant Memory"].append(item)
    lines = ["# Memory Context", "", "Use this scoped context only when relevant. Do not treat candidates as verified facts."]
    used = estimate_tokens("\n".join(lines))
    for heading, items in sections.items():
        if not items:
            continue
        block_lines = ["", f"## {heading}"]
        for item in items:
            block = render_item(item)
            if used + estimate_tokens("\n".join(block_lines + [block])) > budget:
                continue
            block_lines.extend(["", block])
            used += estimate_tokens(block)
        if len(block_lines) > 2:
            lines.extend(block_lines)
    conflicts = [item for item in selected if str(item.get("status", "")).casefold() in {"corrected", "superseded"}]
    if conflicts:
        lines.extend(["", "## Conflicts"])
        lines.extend(f"- {item.get('title', 'memory')} is {item.get('status')} and excluded from current facts." for item in conflicts)
    evidence = list((raw_evidence or {}).get("results", []))[:3]
    if evidence and used < budget:
        lines.extend(["", "## Raw Evidence"])
        for item in evidence:
            snippet = compact(str(item.get("snippet", "")), 220)
            candidate = f"- raw_event:{item.get('id', '')} | {item.get('effective_time', '')} | {snippet}"
            if used + estimate_tokens(candidate) <= budget:
                lines.append(candidate)
                used += estimate_tokens(candidate)
    if len(lines) == 3:
        lines.extend(["", "## Current Relevant Memory", "- No relevant structured memories were found."])
    return "\n".join(lines).strip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Render bounded context from retrieval JSON.")
    parser.add_argument("--retrieved-file", required=True)
    parser.add_argument("--raw-evidence-file")
    parser.add_argument("--token-budget", type=int)
    parser.add_argument("--out-file")
    args = parser.parse_args()
    retrieved = json.loads(Path(args.retrieved_file).read_text(encoding="utf-8-sig"))
    evidence = json.loads(Path(args.raw_evidence_file).read_text(encoding="utf-8-sig")) if args.raw_evidence_file else None
    context = assemble_context(retrieved, evidence, token_budget=args.token_budget)
    if args.out_file:
        Path(args.out_file).write_text(context, encoding="utf-8")
    print(context, end="")


if __name__ == "__main__":
    main()
