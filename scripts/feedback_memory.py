#!/usr/bin/env python3
"""Feedback is source-backed evidence; it never fabricates a correction."""
from __future__ import annotations

import argparse

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root
from ingest_raw_event import insert_raw_event
from proposal_manager import stage_memory_proposal


FEEDBACK_WEIGHT = {"used":.02,"helpful":.05,"user_confirmed":.10,"unhelpful":-.08,"outdated":-.12,"incorrect":-.20,"irrelevant":-.04}


def record_feedback(root, *, claim_id: str, feedback_type: str, source: str = "user", note: str = "", retrieval_uid: str = "") -> dict[str, object]:
    if feedback_type not in FEEDBACK_WEIGHT: raise ValueError(f"Unsupported feedback type: {feedback_type}")
    conn=open_db(root)
    claim=conn.execute("SELECT id,subject_id,subject_name,memory_kind,domain,topic,title,content,confidence,importance,durability,sensitivity,predicate,subject_text,object_text FROM claims WHERE id=?",(claim_id,)).fetchone()
    if not claim: conn.close(); raise ValueError("Claim not found")
    conn.close()
    raw_event_id = None
    if note.strip():
        raw_event_id = int(insert_raw_event(root, subject_id=str(claim[1]), subject_name=str(claim[2] or "Unknown"), source_type="user-memory-feedback", content=note.strip(), allow_duplicate=True)["raw_event_id"])
    conn=open_db(root)
    conn.execute("INSERT INTO memory_feedback(claim_uid,retrieval_uid,feedback_type,source,weight,note,raw_event_id) VALUES(?, ?, ?, ?, ?, ?, ?)",(claim_id,retrieval_uid or None,feedback_type,source,FEEDBACK_WEIGHT[feedback_type],note,raw_event_id))
    conn.execute("UPDATE claims SET confirmed_utility=MAX(-1.0,MIN(1.0,confirmed_utility+?)),updated_at=CURRENT_TIMESTAMP WHERE id=?",(FEEDBACK_WEIGHT[feedback_type],claim_id))
    proposal_id, status = "", "recorded"
    if feedback_type in {"incorrect","outdated"}:
        action = "CORRECT" if feedback_type=="incorrect" else "SUPERSEDE"
        sources=[int(row[0]) for row in conn.execute("SELECT raw_event_id FROM claim_sources WHERE claim_id=? ORDER BY raw_event_id",(claim_id,))]
        if raw_event_id: sources=list(dict.fromkeys(sources+[raw_event_id]))
        plan={"plan_id":f"feedback:{claim_id}:{feedback_type}:{raw_event_id or 'clarify'}","action":action if raw_event_id else f"REVIEW_{action}","subject_id":str(claim[1]),"target_claim_id":claim_id,"source_event_ids":sources,"memory_kind":str(claim[3]),"domain":str(claim[4]),"topic":str(claim[5]),"title":str(claim[6]),"content":note.strip(),"confidence":float(claim[8]),"importance":float(claim[9]),"durability":float(claim[10]),"sensitivity":str(claim[11]),"predicate":str(claim[12]),"subject_text":str(claim[13]),"object_text":str(claim[14]),"requires_review":True,"risk_reason":f"feedback:{feedback_type}"}
        proposal_id=stage_memory_proposal(conn,plan,origin="feedback",reason=f"feedback_{feedback_type}")
        if not raw_event_id:
            conn.execute("UPDATE write_proposals SET status='needs_clarification',review_note='Feedback needs replacement content before it can alter a claim.' WHERE proposal_uid=?",(proposal_id,)); status="needs_clarification"
    conn.commit(); conn.close()
    return {"status":"ok","claim_id":claim_id,"feedback_type":feedback_type,"weight":FEEDBACK_WEIGHT[feedback_type],"raw_event_id":raw_event_id,"proposal_id":proposal_id,"proposal_status":status}


def main() -> None:
    parser=argparse.ArgumentParser(description="Record explicit, source-backed memory feedback.")
    parser.add_argument("--store",help=DEFAULT_STORE_HELP);parser.add_argument("--claim-id",required=True);parser.add_argument("--type",required=True,choices=sorted(FEEDBACK_WEIGHT));parser.add_argument("--source",default="user");parser.add_argument("--note",default="");parser.add_argument("--retrieval-id",default="")
    args=parser.parse_args();emit(record_feedback(store_root(args.store),claim_id=args.claim_id,feedback_type=args.type,source=args.source,note=args.note,retrieval_uid=args.retrieval_id))


if __name__=="__main__":main()
