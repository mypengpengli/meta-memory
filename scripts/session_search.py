#!/usr/bin/env python3
"""FTS-first archive discovery, scoped by subject/workspace/profile."""
from __future__ import annotations

import argparse
import re

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root
from session_archive import HIDDEN_SOURCES, SOURCE_PRIORITY


def _terms(query: str) -> list[str]:
    result=set(re.findall(r"[a-z0-9][\w.-]{1,}",query.casefold()))
    for run in re.findall(r"[\u4e00-\u9fff]{2,}",query):
        result.add(run);result.update(run[index:index+2] for index in range(len(run)-1))
    return sorted(result,key=len,reverse=True)[:18]
def _fts_query(terms: list[str]) -> str: return " OR ".join('"'+term.replace('"','""')+'"' for term in terms)
def _root(lineage: dict[str,str], value: str) -> str:
    seen=set()
    while lineage.get(value) and value not in seen: seen.add(value);value=lineage[value]
    return value
def _message(row): return {"id":int(row[0]),"session_id":str(row[1]),"role":str(row[2]),"content":str(row[3]),"tool_name":str(row[4] or ""),"timestamp":str(row[5])}


def _scroll_conn(conn, session_id: str, anchor: int, window: int) -> list[dict[str,object]]:
    ids=[int(row[0]) for row in conn.execute("SELECT id FROM session_messages WHERE session_id=? AND id<=? ORDER BY id DESC LIMIT ?",(session_id,anchor,max(1,window+1)))]
    ids += [int(row[0]) for row in conn.execute("SELECT id FROM session_messages WHERE session_id=? AND id>? ORDER BY id LIMIT ?",(session_id,anchor,max(0,window)))]
    if not ids:return []
    rows=conn.execute("SELECT id,session_id,role,content,tool_name,timestamp FROM session_messages WHERE id IN ({}) ORDER BY id".format(", ".join("?" for _ in ids)),sorted(set(ids))).fetchall();return [_message(row) for row in rows]


def discovery(root, *, subject_id: str, query: str, limit: int=10, include_hidden: bool=False, workspace_id: str="default", profile_id: str="default") -> dict[str,object]:
    conn=open_db(root); terms=_terms(query); lineage={str(row[0]):str(row[1] or "") for row in conn.execute("SELECT session_id,parent_session_id FROM sessions WHERE subject_id=? AND workspace_id=? AND profile_id=?",(subject_id,workspace_id,profile_id))}
    rows=[]; fts=_fts_query(terms)
    if fts:
        try:
            rows=conn.execute("""SELECT m.id,m.session_id,m.role,m.content,m.tool_name,m.timestamp,s.source,s.title,s.last_active_at,bm25(session_messages_fts)
                FROM session_messages_fts JOIN session_messages m ON m.id=session_messages_fts.rowid JOIN sessions s ON s.session_id=m.session_id
                WHERE session_messages_fts MATCH ? AND s.subject_id=? AND s.workspace_id=? AND s.profile_id=? ORDER BY bm25(session_messages_fts) LIMIT ?""",(fts,subject_id,workspace_id,profile_id,max(limit*8,20))).fetchall()
        except Exception: rows=[]
    if not rows and terms:
        clauses=" OR ".join("LOWER(m.content) LIKE ?" for _ in terms);rows=conn.execute(f"SELECT m.id,m.session_id,m.role,m.content,m.tool_name,m.timestamp,s.source,s.title,s.last_active_at,0 FROM session_messages m JOIN sessions s ON s.session_id=m.session_id WHERE s.subject_id=? AND s.workspace_id=? AND s.profile_id=? AND ({clauses}) ORDER BY s.last_active_at DESC,m.id DESC LIMIT ?",(subject_id,workspace_id,profile_id,*[f"%{term}%" for term in terms],max(limit*8,20))).fetchall()
    grouped={}
    for row in rows:
        source=str(row[6] or "interactive")
        if not include_hidden and source in HIDDEN_SOURCES:continue
        root_id=_root(lineage,str(row[1]));score=SOURCE_PRIORITY.get(source,.5)+min(.3,sum(term in str(row[3]).casefold() for term in terms)*.05)-max(0.,float(row[9] or 0))*0.001
        if root_id not in grouped or score>grouped[root_id]["_score"]:grouped[root_id]={"session_id":str(row[1]),"lineage_root":root_id,"title":str(row[7] or ""),"source":source,"match_message_id":int(row[0]),"match_snippet":" ".join(str(row[3]).split())[:280],"last_active_at":str(row[8]),"_score":score}
    results=sorted(grouped.values(),key=lambda item:(item["_score"],item["last_active_at"]),reverse=True)[:limit]
    for item in results:item.pop("_score",None);item["window"]=_scroll_conn(conn,item["session_id"],int(item["match_message_id"]),2)
    conn.close();return {"status":"ok","mode":"discovery","query":query,"fts_used":bool(rows and fts),"sessions":results}


def scroll(root, *, session_id: str, around_message_id: int, window: int=6, subject_id: str="", workspace_id: str="default", profile_id: str="default") -> dict[str,object]:
    conn=open_db(root)
    internal = session_id
    if subject_id:
        row = conn.execute("SELECT session_id FROM sessions WHERE session_id=? AND subject_id=? AND workspace_id=? AND profile_id=?", (session_id, subject_id, workspace_id, profile_id)).fetchone()
        if not row:
            row = conn.execute("SELECT session_id FROM sessions WHERE external_session_id=? AND subject_id=? AND workspace_id=? AND profile_id=?", (session_id, subject_id, workspace_id, profile_id)).fetchone()
        if not row:
            conn.close(); return {"status":"not_found","mode":"scroll","session_id":session_id,"messages":[]}
        internal = str(row[0])
    messages=_scroll_conn(conn,internal,around_message_id,window);conn.close();return {"status":"ok","mode":"scroll","session_id":internal,"messages":messages}
def browse(root, *, subject_id: str, recent: int=20, include_hidden: bool=False, workspace_id: str="default", profile_id: str="default") -> dict[str,object]:
    conn=open_db(root);rows=conn.execute("SELECT session_id,parent_session_id,external_session_id,source,title,started_at,last_active_at,status FROM sessions WHERE subject_id=? AND workspace_id=? AND profile_id=? ORDER BY last_active_at DESC LIMIT ?",(subject_id,workspace_id,profile_id,max(recent*3,recent))).fetchall();lineage={str(row[0]):str(row[1] or "") for row in rows};seen=set();results=[]
    for row in rows:
        if not include_hidden and str(row[3] or "interactive") in HIDDEN_SOURCES:continue
        root_id=_root(lineage,str(row[0]));
        if root_id in seen:continue
        seen.add(root_id);results.append({"session_id":str(row[0]),"external_session_id":str(row[2] or ""),"lineage_root":root_id,"source":str(row[3]),"title":str(row[4] or ""),"started_at":str(row[5]),"last_active_at":str(row[6]),"status":str(row[7])})
        if len(results)>=recent:break
    conn.close();return {"status":"ok","mode":"browse","sessions":results}


def main() -> None:
    parser=argparse.ArgumentParser(description="Search original messages with FTS and scope isolation.");parser.add_argument("--store",help=DEFAULT_STORE_HELP);parser.add_argument("--subject-id");parser.add_argument("--workspace-id",default="default");parser.add_argument("--profile-id",default="default");parser.add_argument("--query");parser.add_argument("--session-id");parser.add_argument("--around-message-id",type=int);parser.add_argument("--window",type=int,default=6);parser.add_argument("--recent",type=int);parser.add_argument("--include-hidden",action="store_true")
    args=parser.parse_args();root=store_root(args.store)
    if args.session_id and args.around_message_id is not None:emit(scroll(root,session_id=args.session_id,around_message_id=args.around_message_id,window=args.window,subject_id=args.subject_id or "",workspace_id=args.workspace_id,profile_id=args.profile_id))
    elif args.recent is not None and args.subject_id:emit(browse(root,subject_id=args.subject_id,recent=args.recent,include_hidden=args.include_hidden,workspace_id=args.workspace_id,profile_id=args.profile_id))
    elif args.query and args.subject_id:emit(discovery(root,subject_id=args.subject_id,query=args.query,include_hidden=args.include_hidden,workspace_id=args.workspace_id,profile_id=args.profile_id))
    else:raise SystemExit("Use --subject-id with --query/--recent, or --session-id with --around-message-id.")

if __name__=="__main__":main()
