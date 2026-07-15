#!/usr/bin/env python3
"""FTS-first archive discovery, scoped by subject/workspace/profile."""
from __future__ import annotations

import argparse
import json
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


def _event_visibility_sql(*, alias: str = "m") -> str:
    """Exclude pending/current and non-conversational evidence by default."""
    return (
        f"({alias}.role IN ('user','assistant') AND ("
        f"{alias}.raw_event_id IS NULL OR EXISTS ("
        "SELECT 1 FROM raw_events AS r "
        f"WHERE r.id={alias}.raw_event_id "
        "AND REPLACE(LOWER(COALESCE(r.source_type,'')), '_', '-') NOT LIKE '%resource%' "
        "AND REPLACE(LOWER(COALESCE(r.source_type,'')), '_', '-') NOT LIKE '%agent-observation%' "
        "AND REPLACE(LOWER(COALESCE(r.source_type,'')), '_', '-') NOT LIKE '%tool-result%' "
        "AND REPLACE(LOWER(COALESCE(r.source_type,'')), '_', '-') NOT LIKE '%subagent%'"
        ")))"
    )


def _scroll_conn(conn, session_id: str, anchor: int, window: int, *, include_hidden: bool = False) -> list[dict[str,object]]:
    ids=[int(row[0]) for row in conn.execute("SELECT id FROM session_messages WHERE session_id=? AND id<=? ORDER BY id DESC LIMIT ?",(session_id,anchor,max(1,window+1)))]
    ids += [int(row[0]) for row in conn.execute("SELECT id FROM session_messages WHERE session_id=? AND id>? ORDER BY id LIMIT ?",(session_id,anchor,max(0,window)))]
    if not ids:return []
    visibility = "" if include_hidden else " AND " + _event_visibility_sql()
    rows=conn.execute("SELECT m.id,m.session_id,m.role,m.content,m.tool_name,m.timestamp FROM session_messages AS m WHERE m.id IN ({}){} ORDER BY m.id".format(", ".join("?" for _ in ids), visibility),sorted(set(ids))).fetchall();return [_message(row) for row in rows]


_SECRET = re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|bearer\s+|(?:api[_-]?key|token|cookie|password|secret)\s*[:=]\s*)[^\s,;]+")


def _safe_text(value: object, limit: int) -> str:
    text = _SECRET.sub(r"\1[redacted]", " ".join(str(value or "").split()))
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _questions(value: object) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [_safe_text(item, 280) for item in parsed if str(item).strip()][:8] if isinstance(parsed, list) else []


def _internal_session_id(conn, *, profile_id: str, workspace_id: str, subject_id: str, agent_id: str, external_session_id: str) -> str:
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE profile_id=? AND workspace_id=? AND subject_id=? AND COALESCE(origin_agent_id,'')=? AND external_session_id=? ORDER BY last_active_at DESC LIMIT 1",
        (profile_id, workspace_id, subject_id, agent_id, external_session_id),
    ).fetchone()
    return str(row[0] or "") if row else ""


def discover_session_summaries(
    root,
    *,
    subject_id: str,
    query: str,
    limit: int = 8,
    workspace_id: str = "default",
    profile_id: str = "default",
    agent_id: str = "",
) -> dict[str, object]:
    """Search completed, workspace-visible cards without reading transcripts."""

    conn = open_db(root)
    try:
        clauses = [
            "c.subject_id=?", "c.workspace_id=?", "c.profile_id=?", "c.state='active'",
            "c.completed_turn_count>0", "COALESCE(c.summary_visibility,'workspace')='workspace'",
        ]
        params: list[object] = [subject_id, workspace_id, profile_id]
        if agent_id:
            clauses.append("COALESCE(c.origin_agent_id,'')=?")
            params.append(agent_id)
        terms = _terms(query)
        if terms:
            matches: list[str] = []
            for _ in terms:
                matches.append("(LOWER(COALESCE(c.summary,'')) LIKE ? OR LOWER(COALESCE(c.tool_summary,'')) LIKE ? OR LOWER(COALESCE(c.open_questions,'')) LIKE ?)")
                needle = f"%{_}%"
                params.extend([needle, needle, needle])
            clauses.append("(" + " OR ".join(matches) + ")")
        rows = conn.execute(
            """
            SELECT c.id,c.session_id,c.origin_agent_id,c.summary,c.tool_summary,c.open_questions,
                   c.completed_turn_count,c.last_completed_turn_at,c.updated_at
            FROM session_cards AS c
            WHERE """ + " AND ".join(clauses) + " ORDER BY COALESCE(c.last_completed_turn_at,c.updated_at) DESC LIMIT ?",
            (*params, max(1, limit)),
        ).fetchall()
        sessions = []
        for row in rows:
            external = str(row[1] or "")
            origin = str(row[2] or "")
            sessions.append({
                "card_id": int(row[0]),
                "session_id": external,
                "external_session_id": external,
                "internal_session_id": _internal_session_id(conn, profile_id=profile_id, workspace_id=workspace_id, subject_id=subject_id, agent_id=origin, external_session_id=external),
                "origin_agent_id": origin,
                "summary": _safe_text(row[3], 6000),
                "tool_summary": _safe_text(row[4], 1200),
                "open_questions": _questions(row[5]),
                "completed_turns": int(row[6] or 0),
                "last_completed_turn_at": str(row[7] or ""),
                "updated_at": str(row[8] or ""),
            })
        return {"status": "ok", "mode": "summary", "query": query, "sessions": sessions}
    finally:
        conn.close()


def read_session_detail(
    root,
    *,
    summaries: list[dict[str, object]],
    subject_id: str,
    workspace_id: str,
    profile_id: str,
    max_sessions: int = 3,
    max_turns: int = 8,
    max_chars: int = 12000,
    tool_summary_max_chars: int = 1200,
) -> list[dict[str, object]]:
    """Return only completed user/final-assistant pairs plus safe tool summaries."""

    conn = open_db(root)
    remaining = max(256, max_chars)
    details: list[dict[str, object]] = []
    try:
        for item in summaries[: max(1, max_sessions)]:
            if remaining <= 0:
                break
            external = str(item.get("external_session_id") or item.get("session_id") or "")
            origin = str(item.get("origin_agent_id") or "")
            card = conn.execute(
                "SELECT detail_visibility,tool_summary FROM session_cards WHERE id=? AND subject_id=? AND workspace_id=? AND profile_id=? AND COALESCE(origin_agent_id,'')=?",
                (int(item.get("card_id") or 0), subject_id, workspace_id, profile_id, origin),
            ).fetchone()
            if not card or str(card[0] or "workspace") != "workspace":
                continue
            turns = conn.execute(
                """
                SELECT turn_uid,user_event_id,assistant_event_id,completed_at
                FROM turns WHERE subject_id=? AND profile_id=? AND workspace_id=? AND origin_agent_id=?
                  AND external_session_id=? AND status='completed'
                ORDER BY completed_at DESC LIMIT ?
                """,
                (subject_id, profile_id, workspace_id, origin, external, max(1, max_turns)),
            ).fetchall()
            completed_turns = []
            for turn in reversed(turns):
                messages = []
                for event_id, role in ((turn[1], "user"), (turn[2], "assistant")):
                    if not event_id or remaining <= 0:
                        continue
                    row = conn.execute("SELECT content,created_at FROM raw_events WHERE id=?", (int(event_id),)).fetchone()
                    if not row:
                        continue
                    text = _safe_text(row[0], min(3000, remaining))
                    remaining -= len(text)
                    messages.append({"role": role, "content": text, "timestamp": str(row[1] or "")})
                if messages:
                    completed_turns.append({"turn_id": str(turn[0]), "completed_at": str(turn[3] or ""), "messages": messages})
            details.append({
                "origin_agent_id": origin,
                "external_session_id": external,
                "internal_session_id": str(item.get("internal_session_id") or ""),
                "tool_summary": _safe_text(card[1], max(120, tool_summary_max_chars)),
                "turns": completed_turns,
            })
        return details
    finally:
        conn.close()


def discovery(root, *, subject_id: str, query: str, limit: int=10, include_hidden: bool=False, workspace_id: str="default", profile_id: str="default", agent_id: str="", exclude_session_id: str="") -> dict[str,object]:
    conn=open_db(root); terms=_terms(query); agent_clause=" AND COALESCE(origin_agent_id,'')=?" if agent_id else ""; agent_params=(agent_id,) if agent_id else (); exclude_clause=" AND COALESCE(s.external_session_id,'')!=?" if exclude_session_id else ""; exclude_params=(exclude_session_id,) if exclude_session_id else (); lineage={str(row[0]):str(row[1] or "") for row in conn.execute("SELECT session_id,parent_session_id FROM sessions WHERE subject_id=? AND workspace_id=? AND profile_id=?" + agent_clause,(subject_id,workspace_id,profile_id,*agent_params))}
    rows=[]; fts=_fts_query(terms)
    if fts:
        try:
            rows=conn.execute("""SELECT m.id,m.session_id,m.role,m.content,m.tool_name,m.timestamp,s.source,s.title,s.last_active_at,bm25(session_messages_fts)
                FROM session_messages_fts JOIN session_messages m ON m.id=session_messages_fts.rowid JOIN sessions s ON s.session_id=m.session_id
                WHERE session_messages_fts MATCH ? AND s.subject_id=? AND s.workspace_id=? AND s.profile_id=?
                  """ + agent_clause + exclude_clause + ("" if include_hidden else " AND " + _event_visibility_sql()) + """
                ORDER BY bm25(session_messages_fts) LIMIT ?""",(fts,subject_id,workspace_id,profile_id,*agent_params,*exclude_params,max(limit*8,20))).fetchall()
        except Exception: rows=[]
    if not rows and terms:
        clauses=" OR ".join("LOWER(m.content) LIKE ?" for _ in terms); visibility="" if include_hidden else " AND " + _event_visibility_sql();rows=conn.execute(f"SELECT m.id,m.session_id,m.role,m.content,m.tool_name,m.timestamp,s.source,s.title,s.last_active_at,0 FROM session_messages m JOIN sessions s ON s.session_id=m.session_id WHERE s.subject_id=? AND s.workspace_id=? AND s.profile_id=?{agent_clause}{exclude_clause}{visibility} AND ({clauses}) ORDER BY s.last_active_at DESC,m.id DESC LIMIT ?",(subject_id,workspace_id,profile_id,*agent_params,*exclude_params,*[f"%{term}%" for term in terms],max(limit*8,20))).fetchall()
    grouped={}
    for row in rows:
        source=str(row[6] or "interactive")
        if not include_hidden and source in HIDDEN_SOURCES:continue
        root_id=_root(lineage,str(row[1]));score=SOURCE_PRIORITY.get(source,.5)+min(.3,sum(term in str(row[3]).casefold() for term in terms)*.05)-max(0.,float(row[9] or 0))*0.001
        if root_id not in grouped or score>grouped[root_id]["_score"]:grouped[root_id]={"session_id":str(row[1]),"lineage_root":root_id,"title":str(row[7] or ""),"source":source,"match_message_id":int(row[0]),"match_snippet":" ".join(str(row[3]).split())[:280],"last_active_at":str(row[8]),"_score":score}
    results=sorted(grouped.values(),key=lambda item:(item["_score"],item["last_active_at"]),reverse=True)[:limit]
    for item in results:item.pop("_score",None);item["window"]=_scroll_conn(conn,item["session_id"],int(item["match_message_id"]),2,include_hidden=include_hidden)
    conn.close();return {"status":"ok","mode":"discovery","query":query,"fts_used":bool(rows and fts),"sessions":results}


def scroll(root, *, session_id: str, around_message_id: int, window: int=6, subject_id: str="", workspace_id: str="default", profile_id: str="default", agent_id: str="", include_hidden: bool=False) -> dict[str,object]:
    conn=open_db(root)
    internal = session_id
    if subject_id:
        agent_clause=" AND COALESCE(origin_agent_id,'')=?" if agent_id else ""; agent_params=(agent_id,) if agent_id else ()
        row = conn.execute("SELECT session_id FROM sessions WHERE session_id=? AND subject_id=? AND workspace_id=? AND profile_id=?" + agent_clause, (session_id, subject_id, workspace_id, profile_id, *agent_params)).fetchone()
        if not row:
            row = conn.execute("SELECT session_id FROM sessions WHERE external_session_id=? AND subject_id=? AND workspace_id=? AND profile_id=?" + agent_clause, (session_id, subject_id, workspace_id, profile_id, *agent_params)).fetchone()
        if not row:
            conn.close(); return {"status":"not_found","mode":"scroll","session_id":session_id,"messages":[]}
        internal = str(row[0])
    messages=_scroll_conn(conn,internal,around_message_id,window,include_hidden=include_hidden);conn.close();return {"status":"ok","mode":"scroll","session_id":internal,"messages":messages}
def browse(root, *, subject_id: str, recent: int=20, include_hidden: bool=False, workspace_id: str="default", profile_id: str="default", agent_id: str="") -> dict[str,object]:
    conn=open_db(root);agent_clause=" AND COALESCE(origin_agent_id,'')=?" if agent_id else "";agent_params=(agent_id,) if agent_id else ();rows=conn.execute("SELECT session_id,parent_session_id,external_session_id,source,title,started_at,last_active_at,status FROM sessions WHERE subject_id=? AND workspace_id=? AND profile_id=?" + agent_clause + " ORDER BY last_active_at DESC LIMIT ?",(subject_id,workspace_id,profile_id,*agent_params,max(recent*3,recent))).fetchall();lineage={str(row[0]):str(row[1] or "") for row in rows};seen=set();results=[]
    for row in rows:
        if not include_hidden and str(row[3] or "interactive") in HIDDEN_SOURCES:continue
        root_id=_root(lineage,str(row[0]));
        if root_id in seen:continue
        seen.add(root_id);results.append({"session_id":str(row[0]),"external_session_id":str(row[2] or ""),"lineage_root":root_id,"source":str(row[3]),"title":str(row[4] or ""),"started_at":str(row[5]),"last_active_at":str(row[6]),"status":str(row[7])})
        if len(results)>=recent:break
    conn.close();return {"status":"ok","mode":"browse","sessions":results}


def main() -> None:
    parser=argparse.ArgumentParser(description="Search original messages with FTS and scope isolation.");parser.add_argument("--store",help=DEFAULT_STORE_HELP);parser.add_argument("--subject-id");parser.add_argument("--workspace-id",default="default");parser.add_argument("--profile-id",default="default");parser.add_argument("--agent-id",default="");parser.add_argument("--query");parser.add_argument("--session-id");parser.add_argument("--around-message-id",type=int);parser.add_argument("--window",type=int,default=6);parser.add_argument("--recent",type=int);parser.add_argument("--include-hidden",action="store_true")
    args=parser.parse_args();root=store_root(args.store)
    if args.session_id and args.around_message_id is not None:emit(scroll(root,session_id=args.session_id,around_message_id=args.around_message_id,window=args.window,subject_id=args.subject_id or "",workspace_id=args.workspace_id,profile_id=args.profile_id,agent_id=args.agent_id,include_hidden=args.include_hidden))
    elif args.recent is not None and args.subject_id:emit(browse(root,subject_id=args.subject_id,recent=args.recent,include_hidden=args.include_hidden,workspace_id=args.workspace_id,profile_id=args.profile_id,agent_id=args.agent_id))
    elif args.query and args.subject_id:emit(discovery(root,subject_id=args.subject_id,query=args.query,include_hidden=args.include_hidden,workspace_id=args.workspace_id,profile_id=args.profile_id,agent_id=args.agent_id))
    else:raise SystemExit("Use --subject-id with --query/--recent, or --session-id with --around-message-id.")

if __name__=="__main__":main()
