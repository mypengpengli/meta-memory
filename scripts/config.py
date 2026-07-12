"""Small dependency-free reader for the checked-in default configuration."""
from __future__ import annotations

from pathlib import Path


DEFAULTS: dict[str, object] = {
    "heartbeat.session_flush_min_events": 5,
    "heartbeat.max_events": 20,
    "heartbeat.interval_minutes": 30,
    "consolidation.default_policy": "conservative",
    "consolidation.profile_confidence_threshold": 0.9,
    "consolidation.sensitive_confidence_threshold": 0.97,
    "retrieval.context_token_budget": 1800,
    "retrieval.chunk_chars": 1200,
    "retrieval.chunk_overlap_chars": 150,
    "retrieval.rrf_k": 60,
    "retrieval.enable_embeddings": False,
    "llm.enable_fallback": False,
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


def load_defaults() -> dict[str, object]:
    values = dict(DEFAULTS)
    path = Path(__file__).resolve().parent.parent / "config" / "default.yaml"
    if not path.exists():
        return values
    section = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line or ":" not in line:
            continue
        indent = len(line) - len(line.lstrip())
        key, value = line.strip().split(":", 1)
        if not value.strip():
            section = key.strip() if indent == 0 else section
            continue
        full_key = f"{section}.{key.strip()}" if indent else key.strip()
        values[full_key] = _parse_scalar(value)
    return values


def get(key: str) -> object:
    return load_defaults().get(key, DEFAULTS[key])
