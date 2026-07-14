#!/usr/bin/env python3
"""Compatibility shell for the public, lease-protected maintenance entrypoint."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, store_root


# This compatibility script is intentionally runnable directly from a source
# checkout (``python scripts/run_maintenance.py``), including the CI path.
# Add the repository root before importing the public package rather than
# depending on the caller's working directory or an editable install.
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Meta Memory's unified maintenance cycle.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--config", help="Public Meta Memory config path")
    parser.add_argument("--max-projection-jobs", type=int, default=500)
    parser.add_argument("--skip-projections", action="store_true", help="Retained for CLI compatibility; ignored by the unified cycle")
    parser.add_argument("--shadow-high-risk", action="store_true", help="Retained for CLI compatibility")
    args = parser.parse_args()
    from meta_memory.config import AppConfig, load_config
    from meta_memory.maintenance import maintain

    config = load_config(args.config)
    if args.store:
        config.store = store_root(args.store)
    emit(maintain(config, max_jobs=max(1, args.max_projection_jobs // 20)))


if __name__ == "__main__":
    main()
