#!/usr/bin/env python3
"""Route a query to hot, deep, session, or procedural recall without an LLM."""
from __future__ import annotations

import argparse
import json
import re

from _common import emit


def route_query(query: str) -> dict[str, object]:
    text = (query or "").casefold()
    exact = re.findall(r"(?:[a-z]+[a-z0-9_.:/-]+|\b\d{3,}\b)", text)
    result = {"query_type": "open_ended", "needs_hot_memory": False, "needs_deep_memory": True, "needs_raw_evidence": False, "needs_session_search": False, "needs_historical_facts": False, "needs_procedure": False, "needs_dream_digest": True, "valid_at": None, "entity_hints": [], "exact_tokens": exact[:12]}
    if re.search(r"上次|之前.*(?:讨论|聊天)|we (?:last|previously) (?:discussed|said)", text):
        result.update(query_type="past_conversation", needs_session_search=True, needs_deep_memory=False)
    elif re.search(r"证据|原话|来源|具体什么时候|原始记录|为什么这么记|evidence|source|verbatim|when did", text):
        result.update(query_type="source_evidence", needs_raw_evidence=True, needs_session_search=True)
    elif re.search(r"去年|去年|历史|当时|previously|last year|at that time", text):
        result.update(query_type="historical_fact", needs_historical_facts=True)
    elif re.search(r"偏好|喜欢.*回答|prefer|preference", text):
        result.update(query_type="preference", needs_hot_memory=True)
    elif re.search(r"关系|谁是|relationship", text):
        result.update(query_type="relationship", needs_hot_memory=True)
    elif re.search(r"项目|当前|现在|状态|current|project", text):
        result.update(query_type="project_state", needs_hot_memory=True)
    elif re.search(r"以后|怎么处理|步骤|排查|how should|procedure", text):
        result.update(query_type="procedure", needs_procedure=True)
    elif exact:
        result.update(query_type="exact_technical")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify a recall query deterministically."); parser.add_argument("--query", required=True)
    args = parser.parse_args(); emit(route_query(args.query))


if __name__ == "__main__": main()
