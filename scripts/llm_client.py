"""Optional command-based LLM adapter; disabled unless explicitly configured.

Set META_MEMORY_LLM_COMMAND to a program that reads a JSON request on stdin and
returns a JSON object on stdout. This keeps provider credentials and SDKs out of
the local-first core.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess


def complete(prompt: str, payload: dict[str, object]) -> dict[str, object] | None:
    command = os.environ.get("META_MEMORY_LLM_COMMAND", "").strip()
    if not command:
        return None
    result = subprocess.run(shlex.split(command), input=json.dumps({"prompt": prompt, "payload": payload}, ensure_ascii=False), text=True, capture_output=True, check=True, timeout=45)
    value = json.loads(result.stdout)
    return value if isinstance(value, dict) else None


def embed(texts: list[str]) -> list[list[float]] | None:
    command = os.environ.get("META_MEMORY_EMBEDDINGS_COMMAND", "").strip()
    if not command:
        return None
    result = subprocess.run(shlex.split(command), input=json.dumps({"input": texts}, ensure_ascii=False), text=True, capture_output=True, check=True, timeout=60)
    value = json.loads(result.stdout)
    vectors = value.get("vectors") if isinstance(value, dict) else value
    if not isinstance(vectors, list) or not all(isinstance(vector, list) for vector in vectors):
        return None
    return [[float(item) for item in vector] for vector in vectors]
