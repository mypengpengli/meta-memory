#!/usr/bin/env python3
"""Run the deferred session-card -> unit -> consolidation pipeline.

The default is shadow mode: it reports validated actions without changing durable
claims.  `--apply` is intentionally explicit.
"""
from __future__ import annotations

import argparse

from _common import DEFAULT_STORE_HELP, emit, store_root
from consolidate_memories import build_plan
from extract_memory_units import extract_units


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Meta Memory's deferred consolidation cycle.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--subject-id", required=True)
    parser.add_argument("--policy", choices=["conservative", "balanced", "aggressive"], default="conservative")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--apply", action="store_true", help="Apply low-risk validated actions; default is shadow mode")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = store_root(args.store)
    units = extract_units(root, subject_id=args.subject_id, limit=args.limit)
    plan = build_plan(root, args.subject_id, policy=args.policy, limit=args.limit)
    if args.apply:
        from apply_memory_plan import apply_plan

        result = apply_plan(root, plan)
    else:
        from validate_memory_plan import validate_plan

        result = {"status": "shadow", "validation": validate_plan(root, plan), "actions": plan["actions"]}
    emit({"status": "ok", "units": units, "plan": plan, "result": result})


if __name__ == "__main__":
    main()
