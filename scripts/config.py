"""Small dependency-free reader for the checked-in default configuration."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path


DEFAULTS: dict[str, object] = {
    "heartbeat.session_flush_min_events": 5,
    "heartbeat.max_events": 20,
    "heartbeat.interval_minutes": 30,
    "consolidation.default_policy": "conservative",
    "consolidation.profile_confidence_threshold": 0.9,
    "consolidation.sensitive_confidence_threshold": 0.97,
    "consolidation.correct_confidence_threshold": 0.94,
    "consolidation.supersede_confidence_threshold": 0.90,
    "consolidation.require_two_sources_for_correct": True,
    "hot_memory.enabled": True,
    "hot_memory.user_max_chars": 2400,
    "hot_memory.agent_max_chars": 1800,
    "hot_memory.current_max_chars": 2400,
    "hot_memory.quotas.profile": 5,
    "hot_memory.quotas.state": 3,
    "hot_memory.quotas.goal": 3,
    "hot_memory.quotas.relationship": 2,
    "hot_memory.quotas.domain": 3,
    "hot_memory.frozen_during_session": True,
    "sessions.store_messages": True,
    "sessions.enable_fts": True,
    "review.enabled": True,
    "review.every_n_user_turns": 4,
    "review.recent_messages": 20,
    "review.max_digest_chars": 12000,
    "retrieval.top_k": 8,
    "retrieval.candidate_pool": 50,
    "retrieval.context_token_budget": 1800,
    "retrieval.chunk_chars": 1200,
    "retrieval.chunk_overlap_chars": 150,
    "retrieval.rrf_k": 60,
    "retrieval.enable_embeddings": False,
    "retrieval.weights.field": 0.30,
    "retrieval.weights.document_bm25": 0.15,
    "retrieval.weights.chunk_bm25": 0.20,
    "retrieval.weights.embedding": 0.20,
    "retrieval.weights.entity": 0.10,
    "retrieval.weights.graph": 0.05,
    "llm.enable_fallback": False,
    "security.scan_memory_writes": True,
    "security.scan_prompt_snapshot": True,
    "security.block_invisible_unicode": True,
    "security.block_memory_context_tags": True,
    "worker.enabled": True,
    "worker.retry_limit": 5,
}


def _parse_scalar(value: str) -> object:
    value = value.strip()
    if value.casefold() in {"true", "false"}:
        return value.casefold() == "true"
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value.strip("'\"")


@lru_cache(maxsize=1)
def load_defaults() -> dict[str, object]:
    values = dict(DEFAULTS)
    path = Path(__file__).resolve().parent / "resources" / "default.yaml"
    if not path.exists():
        return values
    sections: list[tuple[int, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line or ":" not in line:
            continue
        indent = len(line) - len(line.lstrip())
        key, value = line.strip().split(":", 1)
        while sections and indent <= sections[-1][0]:
            sections.pop()
        if not value.strip():
            sections.append((indent, key.strip()))
            continue
        prefix = ".".join(name for _, name in sections)
        full_key = f"{prefix}.{key.strip()}" if prefix else key.strip()
        values[full_key] = _parse_scalar(value)
    return values


def get(key: str) -> object:
    values = load_defaults()
    if key in values:
        return values[key]
    if key in DEFAULTS:
        return DEFAULTS[key]
    raise KeyError(f"Unknown Meta Memory configuration key: {key}")


def reload_config() -> None:
    """Explicit opt-in reload for hosts that edit configuration at runtime."""
    load_defaults.cache_clear()
