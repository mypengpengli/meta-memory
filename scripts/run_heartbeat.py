#!/usr/bin/env python3
"""Heartbeat only schedules durable work; it never performs the heavy path."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root
from background_review import enqueue_review


def parse_args() -> argparse.Namespace:
    parser=argparse.ArgumentParser(description="Schedule review jobs for pending raw evidence.");parser.add_argument("--store",help=DEFAULT_STORE_HELP);parser.add_argument("--subject-id");parser.add_argument("--interval-minutes",type=int,default=30);parser.add_argument("--min-pending",type=int,default=5);parser.add_argument("--max-events",type=int,default=100);parser.add_argument("--policy",choices=["conservative","balanced","aggressive"],default="conservative");parser.add_argument("--dry-run",action="store_true")
    return parser.parse_args()
def parse_time(value):
    try:return datetime.fromisoformat(str(value).replace(" ","T")).astimezone(timezone.utc) if value else None
    except ValueError:return None
def run_heartbeat(root, *, subject_id: str|None=None, interval_minutes: int=30, min_pending: int=5, dry_run: bool=False) -> dict[str,object]:
    conn=open_db(root);clauses=["processed_state='pending'"];params=[]
    if subject_id:clauses.append("subject_id=?");params.append(subject_id)
    rows=conn.execute(f"SELECT subject_id,COALESCE(session_id,''),COUNT(*),MIN(id),MAX(id),(SELECT last_heartbeat_at FROM maintenance_cursor c WHERE c.subject_id=raw_events.subject_id) FROM raw_events WHERE {' AND '.join(clauses)} GROUP BY subject_id,COALESCE(session_id,'')",params).fetchall();now=datetime.now(timezone.utc);out=[]
    for sid,session,count,start,end,last_flush in rows:
        previous=parse_time(last_flush);due=int(count)>=min_pending or previous is None or now-previous>=timedelta(minutes=interval_minutes)
        if due and not dry_run:
            job=enqueue_review(root,subject_id=str(sid),session_id=str(session),event_start_id=int(start),event_end_id=int(end),trigger_type="heartbeat")
            conn.execute("INSERT INTO maintenance_cursor(subject_id,last_heartbeat_at,last_check_at) VALUES(?, ?, ?) ON CONFLICT(subject_id) DO UPDATE SET last_heartbeat_at=excluded.last_heartbeat_at,last_check_at=excluded.last_check_at",(sid,now.isoformat(),now.isoformat()))
        else:
            job=None
            if not dry_run:conn.execute("INSERT INTO maintenance_cursor(subject_id,last_check_at) VALUES(?, ?) ON CONFLICT(subject_id) DO UPDATE SET last_check_at=excluded.last_check_at",(sid,now.isoformat()))
        out.append({"subject_id":sid,"session_id":session,"pending_count":count,"scheduled":bool(due),"job":job})
    conn.commit();conn.close();return {"status":"ok","subjects":out,"dry_run":dry_run}
def main() -> None:
    args=parse_args();emit(run_heartbeat(store_root(args.store),subject_id=args.subject_id,interval_minutes=args.interval_minutes,min_pending=args.min_pending,dry_run=args.dry_run))
if __name__=="__main__":main()
