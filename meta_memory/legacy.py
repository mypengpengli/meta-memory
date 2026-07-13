"""Bridge the compact public CLI to the proven legacy implementation.

The repository deliberately keeps the mature data-plane modules in
``scripts/`` while the public package owns configuration and user experience.
Adding that directory to ``sys.path`` is temporary compatibility glue, not a
second public API.
"""
from __future__ import annotations

import sys
from pathlib import Path


def legacy_root() -> Path:
    """Return the directory that contains the internal implementation."""
    local = Path(__file__).resolve().parents[1] / "scripts"
    if local.is_dir():
        return local
    # In a wheel the compatibility modules are installed as ``scripts``.
    import scripts  # type: ignore

    return Path(scripts.__file__).resolve().parent


def bootstrap() -> Path:
    root = legacy_root()
    text = str(root)
    if text not in sys.path:
        sys.path.insert(0, text)
    return root
