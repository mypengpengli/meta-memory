#!/usr/bin/env python3
"""SQLite-leased durable review worker; no in-process secondary queue."""
from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root, utc_now
from apply_memory_plan import apply_plan
from build_session_card import build_cards
from config import get
from consolidate_memories import build_plan
from extract_memory_units import extract_units


def enqueue_review(
    root,
    *,
    subject_id: str,
    session_id: str,
    event_start_id: int,
    event_end_id: int,
    trigger_type: str,
    profile_id: str = "default",
    workspace_id: str = "default",
    origin_agent_id: str = "",
    conn=None,
) -> dict[str, object]:
    """Keep one pending tail per identity/session instead of overlapping jobs.

    Supplying a connection lets a caller make event insertion, turn completion,
    and review-job creation one SQLite transaction.  The compatibility path
    still owns its own connection and commits exactly as before.
    """
    own_connection = conn is None
    conn = conn or open_db(root)
    try:
        pending = conn.execute(
            "SELECT job_uid,event_start_id,event_end_id FROM review_jobs "
            "WHERE subject_id=? AND session_id=? AND profile_id=? AND workspace_id=? "
            "AND origin_agent_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
            (subject_id, session_id, profile_id, workspace_id, origin_agent_id),
        ).fetchone()
        if pending:
            start = min(int(pending[1] or event_start_id), event_start_id) if int(pending[1] or 0) else event_start_id
            end = max(int(pending[2] or 0), event_end_id)
            key = f"{subject_id}:{profile_id}:{workspace_id}:{origin_agent_id}:{session_id}:{start}:{end}:{trigger_type}:v3"
            conn.execute(
                "UPDATE review_jobs SET event_start_id=?,event_end_id=?,job_key=?,trigger_type=? WHERE job_uid=?",
                (start, end, key, trigger_type, pending[0]),
            )
            if own_connection:
                conn.commit()
            return {"job_id": str(pending[0]), "status": "pending", "deduplicated": True, "merged_tail": True}

        key = f"{subject_id}:{profile_id}:{workspace_id}:{origin_agent_id}:{session_id}:{event_start_id}:{event_end_id}:{trigger_type}:v3"
        existing = conn.execute("SELECT job_uid,status FROM review_jobs WHERE job_key=?", (key,)).fetchone()
        if existing:
            return {"job_id": str(existing[0]), "status": str(existing[1]), "deduplicated": True}

        uid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO review_jobs(job_uid,job_key,subject_id,session_id,event_start_id,event_end_id,trigger_type,profile_id,workspace_id,origin_agent_id) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, key, subject_id, session_id, event_start_id, event_end_id, trigger_type, profile_id, workspace_id, origin_agent_id),
        )
        if own_connection:
            conn.commit()
        return {"job_id": uid, "status": "pending", "deduplicated": False}
    finally:
        if own_connection:
            conn.close()


def recover_stuck_jobs(root, *, timeout_minutes: int = 10) -> int:
    conn=open_db(root); cutoff=(datetime.now(timezone.utc)-timedelta(minutes=timeout_minutes)).isoformat()
    changed=conn.execute("UPDATE review_jobs SET status='pending',lease_owner=NULL,leased_until=NULL,started_at=NULL,next_retry_at=? WHERE status='running' AND (leased_until<? OR started_at<?)",(utc_now(),cutoff,cutoff)).rowcount; conn.commit();conn.close();return int(changed)


def _claim_job(root, worker_id: str):
    conn=open_db(root); now=utc_now(); lease=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()
    conn.execute("BEGIN IMMEDIATE")
    row=conn.execute("SELECT job_uid,subject_id,session_id,event_start_id,event_end_id,attempt_count,profile_id,workspace_id,origin_agent_id FROM review_jobs WHERE status='pending' AND (next_retry_at IS NULL OR next_retry_at<=?) AND (leased_until IS NULL OR leased_until<?) ORDER BY created_at LIMIT 1",(now,now)).fetchone()
    if not row: conn.commit();conn.close();return None
    claimed=conn.execute("UPDATE review_jobs SET status='running',attempt_count=attempt_count+1,started_at=?,lease_owner=?,leased_until=? WHERE job_uid=? AND status='pending'",(now,worker_id,lease,row[0])).rowcount
    conn.commit();conn.close(); return tuple(row) if claimed else None


def renew_lease(root, job_uid: str, worker_id: str, *, minutes: int = 5) -> bool:
    until=(datetime.now(timezone.utc)+timedelta(minutes=minutes)).isoformat();conn=open_db(root)
    changed=conn.execute("UPDATE review_jobs SET leased_until=? WHERE job_uid=? AND status='running' AND lease_owner=?",(until,job_uid,worker_id)).rowcount
    conn.commit();conn.close();return bool(changed)


def _digest(root, subject_id: str, session_id: str) -> str:
    conn=open_db(root); rows=conn.execute("SELECT role,content FROM session_messages m JOIN sessions s ON s.session_id=m.session_id WHERE s.subject_id=? AND s.external_session_id=? ORDER BY m.id DESC LIMIT ?",(subject_id,session_id, int(get("review.recent_messages")))).fetchall();conn.close(); return "\n".join(f"{row[0]}: {str(row[1])[:500]}" for row in reversed(rows))[:int(get("review.max_digest_chars"))]


def _finish(root, uid: str, *, status: str, plan: dict[str,object] | None=None, error: str="", retry: bool=False) -> None:
    conn=open_db(root)
    if retry:
        attempts=int(conn.execute("SELECT attempt_count FROM review_jobs WHERE job_uid=?",(uid,)).fetchone()[0]); limit=int(get("worker.retry_limit")); delay=min(3600,30*(2**max(0,attempts-1)))
        if attempts>=limit: status, next_retry="dead_letter",None
        else: status,next_retry="pending",(datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat()
        conn.execute("UPDATE review_jobs SET status=?,next_retry_at=?,last_error=?,completed_at=NULL,lease_owner=NULL,leased_until=NULL WHERE job_uid=?",(status,next_retry,error,uid))
    else:
        conn.execute("UPDATE review_jobs SET status=?,memory_plan_json=?,completed_at=?,last_error=?,lease_owner=NULL,leased_until=NULL WHERE job_uid=?",(status,json.dumps(plan or {},ensure_ascii=False),utc_now(),error or None,uid))
    conn.commit();conn.close()


def run_pending(root, *, max_jobs: int=10, policy: str="conservative", apply_low_risk: bool=False, worker_id: str="") -> dict[str,object]:
    worker_id=worker_id or f"worker:{uuid.uuid4()}"; results=[]
    for _ in range(max(0,max_jobs)):
        job=_claim_job(root,worker_id)
        if not job: break
        uid,subject_id,session_id,start,end,_,profile_id,workspace_id,origin_agent_id=job
        try:
            cards=build_cards(root,subject_id=str(subject_id),session_id=str(session_id) or None,event_start_id=int(start or 0) or None,event_end_id=int(end or 0) or None,force=True,profile_id=str(profile_id),workspace_id=str(workspace_id),origin_agent_id=str(origin_agent_id))
            renew_lease(root,str(uid),worker_id)
            card_ids=[int(card["card_id"]) for card in cards["cards"] if card.get("card_id")]
            units=extract_units(root,subject_id=str(subject_id),card_ids=card_ids,event_start_id=int(start or 0) or None,event_end_id=int(end or 0) or None,limit=max(1,len(card_ids) or 1),profile_id=str(profile_id),workspace_id=str(workspace_id),origin_agent_id=str(origin_agent_id),owner_agent_id=str(origin_agent_id))
            renew_lease(root,str(uid),worker_id)
            unit_ids=[int(item["unit_id"]) for item in units["created"] if item.get("unit_id")]
            plan=build_plan(root,str(subject_id),policy=policy,unit_ids=unit_ids,limit=max(1,len(unit_ids) or 1),profile_id=str(profile_id),workspace_id=str(workspace_id),origin_agent_id=str(origin_agent_id))
            for action in plan["actions"]:
                action.update({"origin":"background_review","session_id":str(session_id),"profile_id":str(profile_id),"workspace_id":str(workspace_id),"origin_agent_id":str(origin_agent_id)})
                # Automatic memory is deliberately narrow: only a verified,
                # normal-sensitivity CREATE without a conflict can bypass the
                # review queue. Source events are user-only because the
                # extractor excludes assistant content by default.
                if apply_low_risk and str(action.get("action")) == "CREATE" and str(action.get("verification_state")) == "verified" and str(action.get("sensitivity")) == "normal" and not bool(action.get("requires_review")):
                    action["auto_promote"] = True
            outcome=apply_plan(root,plan,skip_index=True) if apply_low_risk and plan["actions"] else {"status":"shadow","actions":plan["actions"]}
            status="applied" if apply_low_risk and outcome.get("status")=="ok" else "staged" if plan["actions"] else "planned"; _finish(root,str(uid),status=status,plan=plan);results.append({"job_id":uid,"status":status,"card_ids":card_ids,"unit_ids":unit_ids,"outcome":outcome})
        except Exception as exc:
            _finish(root,str(uid),status="pending",error=str(exc),retry=True);results.append({"job_id":uid,"status":"retrying","error":str(exc)})
    return {"status":"ok","worker_id":worker_id,"results":results}


def main() -> None:
    parser=argparse.ArgumentParser(description="Run the single durable review worker.");parser.add_argument("--store",help=DEFAULT_STORE_HELP);parser.add_argument("--subject-id");parser.add_argument("--session-id",default="");parser.add_argument("--event-start-id",type=int,default=0);parser.add_argument("--event-end-id",type=int,default=0);parser.add_argument("--trigger",default="scheduled_maintenance");parser.add_argument("--profile-id",default="default");parser.add_argument("--workspace-id",default="default");parser.add_argument("--agent-id",default="");parser.add_argument("--run",action="store_true");parser.add_argument("--loop",action="store_true");parser.add_argument("--poll-seconds",type=float,default=2);parser.add_argument("--max-jobs",type=int,default=10);parser.add_argument("--apply-low-risk",action="store_true")
    args=parser.parse_args();root=store_root(args.store)
    if args.loop:
        while True: run_pending(root,max_jobs=args.max_jobs,apply_low_risk=args.apply_low_risk); time.sleep(max(.1,args.poll_seconds))
    elif args.run: emit(run_pending(root,max_jobs=args.max_jobs,apply_low_risk=args.apply_low_risk))
    elif args.subject_id: emit(enqueue_review(root,subject_id=args.subject_id,session_id=args.session_id,event_start_id=args.event_start_id,event_end_id=args.event_end_id,trigger_type=args.trigger,profile_id=args.profile_id,workspace_id=args.workspace_id,origin_agent_id=args.agent_id))
    else: raise SystemExit("Use --run/--loop or supply --subject-id to queue a review job.")


if __name__=="__main__":main()
