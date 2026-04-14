#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from _common import emit


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the heartbeat organizer on a repeating timer so the memory system can organize incrementally."
    )
    parser.add_argument("--store", required=True, help="Path to the external memory-data root")
    parser.add_argument("--subject-id", help="Limit to one subject_id")
    parser.add_argument("--check-every-minutes", type=float, default=10.0, help="Heartbeat check interval")
    parser.add_argument("--organize-interval-minutes", type=int, default=30, help="Minimum organize interval")
    parser.add_argument("--min-pending", type=int, default=3, help="Pending raw event threshold")
    parser.add_argument("--max-events", type=int, default=20, help="Maximum raw events per organize batch")
    parser.add_argument(
        "--policy",
        choices=["conservative", "balanced", "aggressive"],
        default="balanced",
        help="How aggressively to write directly into long-term layers",
    )
    parser.add_argument("--skip-index", action="store_true", help="Skip the reindex/rescore pass")
    parser.add_argument("--run-once", action="store_true", help="Run a single heartbeat tick and exit")
    parser.add_argument("--iterations", type=int, help="Maximum ticks to run before exiting")
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_command(args: argparse.Namespace) -> list[str]:
    base = Path(__file__).resolve().parent
    command = [
        sys.executable,
        str(base / "run_heartbeat.py"),
        "--store",
        args.store,
        "--interval-minutes",
        str(args.organize_interval_minutes),
        "--min-pending",
        str(args.min_pending),
        "--max-events",
        str(args.max_events),
        "--policy",
        args.policy,
    ]
    if args.subject_id:
        command.extend(["--subject-id", args.subject_id])
    if args.skip_index:
        command.append("--skip-index")
    return command


def run_tick(args: argparse.Namespace) -> dict[str, object]:
    result = subprocess.run(
        build_command(args),
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    payload["tick_started_at"] = utc_now()
    return payload


def main() -> None:
    args = parse_args()
    ticks: list[dict[str, object]] = []
    tick_count = 0

    while True:
        tick_count += 1
        payload = run_tick(args)
        payload["tick"] = tick_count
        ticks.append(payload)

        if args.run_once or (args.iterations and tick_count >= args.iterations):
            break

        time.sleep(max(1.0, args.check_every_minutes * 60.0))

    emit(
        {
            "status": "ok",
            "ticks": tick_count,
            "check_every_minutes": args.check_every_minutes,
            "organize_interval_minutes": args.organize_interval_minutes,
            "min_pending": args.min_pending,
            "policy": args.policy,
            "results": ticks,
        }
    )


if __name__ == "__main__":
    main()
