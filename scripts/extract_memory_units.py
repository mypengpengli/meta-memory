#!/usr/bin/env python3
"""Delta-only, source-timestamped atomic memory unit extraction."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from build_session_card import QUESTION
from classify_memory import classify, first_sentence
from _common import DEFAULT_STORE_HELP, emit, open_db, sha256_text, store_root, utc_now
from meta_memory.scope_inference import inferred_visibility
from llm_client import complete
from runtime_identity import add_identity_args
from validate_memory_units import validate_unit


SENSITIVE = re.compile(r"\b(health|medical|diagnosis|finance|salary|bank|relationship|divorce|password|phone|address)\b|健康|医疗|诊断|财务|工资|银行|关系|离婚|密码|电话|住址")
UNCERTAIN = re.compile(r"\b(maybe|perhaps|might|guess|probably|unsure)\b|可能|也许|大概|猜测|不确定|好像")
ACK = re.compile(r"^(?:ok|okay|thanks|thank you|got it|continue|好的|谢谢|收到|继续)[!！。.]?$", re.I)
BOUNDARY = re.compile(r"(?<=[。！？!?.])\s*|\n+")
EXTRACTION_VERSION = "rules-v2"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract atomic units only from new session-card events.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP); parser.add_argument("--subject-id"); parser.add_argument("--session-id"); parser.add_argument("--card-id", type=int); parser.add_argument("--limit", type=int, default=20); parser.add_argument("--event-start-id", type=int); parser.add_argument("--event-end-id", type=int); parser.add_argument("--include-assistant", action="store_true"); parser.add_argument("--dry-run", action="store_true")
    add_identity_args(parser, include_visibility=True)
    return parser.parse_args()


def is_question(text: str) -> bool:
    compact = text.strip()
    return compact.endswith(("?", "？")) or bool(QUESTION.search(compact))
def contains_assertion(text: str) -> bool:
    # Keep factual clauses embedded in a question such as "The project now
    # uses PostgreSQL; do you remember?".  The localized patterns below cover
    # Chinese phrasing; this explicit English branch avoids an overly narrow
    # "the project uses" match.
    if re.search(r"\bthe project\s+(?:(?:now|currently)\s+)?(?:uses|has|is)\b", text, re.I):
        return True
    return bool(re.search(r"我(?:现在|目前|已经|一直)|项目(?:现在|目前|已经)|\bI (?:currently|now|have|use|prefer)\b|\bthe project (?:uses|has|is)\b", text, re.I))
def sensitivity(text: str) -> str: return "sensitive" if SENSITIVE.search(text) else "normal"
def clamp01(value: object, fallback: float) -> float:
    try: return max(0., min(1., float(value)))
    except (TypeError, ValueError): return fallback
def normalize_dict(value: object) -> dict[str, object]: return {str(key): item for key, item in value.items()} if isinstance(value, dict) else {}
def normalize_entities(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list): return []
    return [{"name":str(item.get("name", "")).strip(),"type":str(item.get("type", "unknown")).strip() or "unknown","role":str(item.get("role", "related")).strip() or "related"} for item in value if isinstance(item, dict) and str(item.get("name", "")).strip()][:20]
def normalize_time(value: object) -> str: return str(value or "").strip()


def atomic_clauses(content: str) -> list[str]:
    result: list[str] = []
    for sentence in BOUNDARY.split(" ".join(content.split())):
        sentence = sentence.strip(" -;；")
        if sentence: result.extend(piece.strip() for piece in re.split(r"\s+(?:and|but)\s+(?=(?:I|we|the |this |now |previously ))", sentence, flags=re.I) if piece.strip())
    return list(dict.fromkeys(result))[:12]


def structured_fields(text: str, topic_hint: str, domain_hint: str) -> dict[str, object]:
    predicate, kind, subject, durability = "states", "domain", "user", .55
    if re.search(r"\b(?:prefer|preference|like responses)\b|偏好|喜欢.*(?:回答|方式)|希望.*(?:回答|说明)", text, re.I): predicate,kind,subject,durability="prefers","profile","user",.9
    elif re.search(r"\b(?:now|currently|migrated|changed to|uses?)\b|现在|目前|已经改成|迁移到", text, re.I): predicate,kind,subject,durability="current_state","state","project",.75
    elif re.search(r"\b(?:previously|used to|before)\b|以前|之前|曾经", text, re.I): predicate,kind,subject,durability="historical_state","event","project",.7
    elif re.search(r"\b(?:please|when troubleshooting|when debugging|when .*?(?:debug|troubleshoot)|first .*? then)\b|以后.*(?:请|先)|排查.*(?:先|再)", text, re.I): predicate,kind,subject,durability="procedure","domain","workflow",.8
    elif re.search(r"\b(?:goal|plan|will|need to)\b|目标|计划|需要", text, re.I): predicate,kind,subject,durability="goal","goal","project",.65
    matched = re.search(r"(?:uses?|use|to|为|是|改成|迁移到)\s+([A-Za-z0-9_.+/#-]{2,}|[\u4e00-\u9fff]{2,})", text, re.I)
    object_text = matched.group(1) if matched else text[:240]
    topic = topic_hint or re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", object_text.casefold()).strip("-") or "memory"
    return {"predicate":predicate,"unit_kind":kind,"subject_text":subject,"object_text":object_text[:240],"topic":topic[:80],"domain":domain_hint or "general","durability":durability,"qualifiers":{},"entities":{},"valid_from":"","valid_to":""}


def optional_llm_units(content: str, raw_event_id: int) -> list[dict[str, object]]:
    if not re.search(r"[。！？!?].+[。！？!?]|\b(?:but|however|previously|now|if)\b|以前|现在|如果|但是", content, re.I): return []
    try: response = complete((Path(__file__).resolve().parent.parent / "prompts" / "extract_memory_units.md").read_text(encoding="utf-8"), {"raw_event_id":raw_event_id,"content":content}) or {}
    except Exception: return []
    values = response.get("units") if isinstance(response, dict) else None
    return [item for item in values or [] if isinstance(item,dict) and item.get("source_event_ids")==[raw_event_id] and str(item.get("claim_text") or "").strip() and str(item.get("predicate") or "").strip()][:12]


def optional_llm_units_batch(events: list[dict[str, object]]) -> dict[int, list[dict[str, object]]]:
    """Use one optional model request for up to ten complex source events."""
    complex_events = [
        {"raw_event_id": int(item["raw_event_id"]), "content": str(item["content"])}
        for item in events
        if re.search(r"[.?!].+[.?!]|\b(?:but|however|previously|now|if)\b", str(item["content"]), re.I)
    ][:10]
    if not complex_events:
        return {}
    try:
        prompt = (Path(__file__).resolve().parent.parent / "prompts" / "extract_memory_units.md").read_text(encoding="utf-8")
        response = complete(prompt, {"events": complex_events, "batch": True}) or {}
    except Exception:
        return {}
    allowed = {entry["raw_event_id"] for entry in complex_events}
    grouped: dict[int, list[dict[str, object]]] = {}
    for item in response.get("units", []) if isinstance(response, dict) else []:
        if not isinstance(item, dict) or not str(item.get("claim_text") or "").strip() or not str(item.get("predicate") or "").strip():
            continue
        source_ids = item.get("source_event_ids")
        if not isinstance(source_ids, list) or len(source_ids) != 1:
            continue
        try:
            event_id = int(source_ids[0])
        except (TypeError, ValueError):
            continue
        if event_id in allowed:
            grouped.setdefault(event_id, []).append(item)
    return {event_id: units[:12] for event_id, units in grouped.items()}


def normalize_extracted_unit(extracted: dict[str, object], *, fallback: dict[str, object], raw_event: dict[str, object]) -> dict[str, object]:
    return {"predicate":str(extracted.get("predicate") or fallback["predicate"]),"unit_kind":str(extracted.get("memory_kind") or fallback["unit_kind"]),"subject_text":str(extracted.get("subject_text") or fallback["subject_text"]),"object_text":str(extracted.get("object_text") or fallback["object_text"]),"topic":str(extracted.get("topic") or fallback["topic"]),"domain":str(extracted.get("domain") or fallback["domain"]),"qualifiers":normalize_dict(extracted.get("qualifiers")),"entities":normalize_entities(extracted.get("entities")),"valid_from":normalize_time(extracted.get("valid_from")),"valid_to":normalize_time(extracted.get("valid_to")),"observed_at":normalize_time(extracted.get("observed_at") or raw_event["event_time"] or raw_event["created_at"]),"durability":clamp01(extracted.get("durability"), float(fallback["durability"]))}


def extract_units(root, *, subject_id: str | None = None, session_id: str | None = None, card_id: int | None = None, card_ids: list[int] | None = None, event_start_id: int | None = None, event_end_id: int | None = None, limit: int = 20, include_assistant: bool = False, dry_run: bool = False, profile_id: str | None = None, workspace_id: str | None = None, origin_agent_id: str = "", visibility_scope: str = "workspace", owner_agent_id: str = "") -> dict[str, object]:
    conn = open_db(root); clauses, params = ["needs_extraction=1"], []
    if subject_id: clauses.append("subject_id=?"); params.append(subject_id)
    if session_id is not None: clauses.append("session_id=?"); params.append(session_id or "__default__")
    if card_ids is not None and not card_ids:
        conn.close(); return {"status":"ok","dry_run":dry_run,"created":[],"skipped":[],"card_count":0}
    identifiers = card_ids if card_ids is not None else ([card_id] if card_id is not None else [])
    if identifiers: clauses.append("id IN ({})".format(", ".join("?" for _ in identifiers))); params.extend(identifiers)
    if profile_id is not None: clauses.append("profile_id=?"); params.append(profile_id)
    if workspace_id is not None: clauses.append("workspace_id=?"); params.append(workspace_id)
    if origin_agent_id: clauses.append("COALESCE(origin_agent_id,'')=?"); params.append(origin_agent_id)
    cards = conn.execute(f"SELECT id,subject_id,subject_name,session_id,last_extracted_event_id,profile_id,workspace_id,origin_agent_id FROM session_cards WHERE {' AND '.join(clauses)} ORDER BY updated_at LIMIT ?", (*params,max(1,limit))).fetchall()
    created, skipped = [], []
    for cid, sid, name, sess, last_extracted, card_profile, card_workspace, card_agent in cards:
        event_clauses, event_params = ["session_card_id=?", "id>?"], [cid, int(last_extracted or 0)]
        if event_start_id is not None: event_clauses.append("id>=?"); event_params.append(event_start_id)
        if event_end_id is not None: event_clauses.append("id<=?"); event_params.append(event_end_id)
        events = conn.execute(f"SELECT id,source_type,content,topic_hint,domain_hint,event_time,created_at,profile_id,workspace_id,visibility_scope,origin_agent_id FROM raw_events WHERE {' AND '.join(event_clauses)} ORDER BY id", event_params).fetchall()
        max_seen = int(last_extracted or 0)
        # Imported resources may contain third-party or historical material.
        # Keep their extraction deterministic and local; they can become only
        # non-prompt candidates, never an LLM-promoted user fact.
        llm_by_event = optional_llm_units_batch([
            {"raw_event_id": int(row[0]), "content": str(row[2] or "")}
            for row in events
            if str(row[1] or "") != "resource"
        ])
        for event_id, source_type, content, topic_hint, domain_hint, event_time, created_at, event_profile, event_workspace, event_visibility, event_agent in events:
            max_seen = max(max_seen,int(event_id)); source_type, content = str(source_type or ""), " ".join(str(content or "").split())[:2000]
            if not content or ACK.match(content): skipped.append({"raw_event_id":event_id,"reason":"empty_or_ack"}); continue
            if source_type=="conversation-assistant" and not include_assistant: skipped.append({"raw_event_id":event_id,"reason":"assistant_content_disabled"}); continue
            raw = {"event_time":str(event_time or ""),"created_at":str(created_at or "")}; llm = llm_by_event.get(int(event_id), []); candidates = llm or [{"claim_text":part} for part in atomic_clauses(content)]
            for clause_index, extracted in enumerate(candidates):
                clause = str(extracted.get("claim_text") or "").strip()
                if not clause or ACK.match(clause) or (is_question(clause) and not contains_assertion(clause)): skipped.append({"raw_event_id":event_id,"reason":"question_or_nonmemory"}); continue
                fallback = structured_fields(clause,str(topic_hint or ""),str(domain_hint or "")); fields = normalize_extracted_unit(extracted if llm else {}, fallback=fallback, raw_event=raw)
                classified = classify(str(topic_hint or first_sentence(clause)[:80] or f"raw-event-{event_id}"), clause, str(sid), str(name or "Unknown")); confidence = clamp01(extracted.get("confidence"), float(classified["suggested_payload"]["confidence"])) if llm else (min(float(classified["suggested_payload"]["confidence"]),.25) if source_type=="conversation-assistant" else float(classified["suggested_payload"]["confidence"]))
                if source_type == "resource":
                    # A resource is evidence, not a statement from the user.
                    # Preserve a reviewable candidate while preventing it from
                    # ever being selected for normal prompt context.
                    fields["unit_kind"] = "candidate"
                    fields["domain"] = "resource"
                    confidence = min(confidence, 0.60)
                base_visibility = str(event_visibility or visibility_scope or "workspace")
                inferred = inferred_visibility(clause, unit_kind=str(fields["unit_kind"]), source_type=source_type)
                unit_visibility = "global" if base_visibility != "agent" and inferred == "global" else base_visibility
                unit_workspace = "global" if unit_visibility == "global" else str(event_workspace or card_workspace)
                if unit_visibility == "global":
                    # A user-wide response/identity preference deserves the
                    # same durable treatment whether it arrived through an
                    # explicit `remember` call or the normal turn pipeline.
                    fields["unit_kind"] = "profile"
                    fields["predicate"] = "prefers"
                    fields["subject_text"] = "user"
                    fields["object_text"] = clause
                    fields["durability"] = max(float(fields["durability"]), 0.90)
                    confidence = max(confidence, 0.90)
                unit = {"subject_id":str(sid),"claim_text":clause,"source_event_ids":[int(event_id)],"memory_kind":fields["unit_kind"],"predicate":fields["predicate"],"confidence":confidence,"uncertainty":clamp01(extracted.get("uncertainty"), .7 if UNCERTAIN.search(clause) else max(.05,1-float(classified["classification_confidence"]))),"importance":clamp01(extracted.get("importance"),float(classified["suggested_payload"]["importance"])),"durability":fields["durability"],"valid_from":fields["valid_from"],"valid_to":fields["valid_to"]}
                validation = validate_unit(root,unit)
                if not validation["valid"]: skipped.append({"raw_event_id":event_id,"reason":"validation","errors":validation["errors"]}); continue
                if dry_run: created.append({"unit_id":None,"raw_event_id":event_id,"clause_index":clause_index,"predicate":fields["predicate"]}); continue
                key=sha256_text(f"{sid}:{event_id}:{clause_index}:{EXTRACTION_VERSION}:{sha256_text(clause)}")
                cursor=conn.execute("""INSERT OR IGNORE INTO memory_units(unit_key,subject_id,subject_name,session_id,session_card_id,raw_event_id,source_event_ids,unit_kind,topic,content,content_hash,confidence,uncertainty,importance,sensitivity,source_type,status,domain,predicate,subject_text,object_text,qualifiers_json,valid_from,valid_to,observed_at,durability,entities_json,security_state,security_findings_json,clause_index,extraction_version,profile_id,workspace_id,visibility_scope,origin_agent_id,owner_agent_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (key,sid,name,sess,cid,event_id,json.dumps([event_id]),fields["unit_kind"],fields["topic"],clause,sha256_text(clause),confidence,unit["uncertainty"],unit["importance"],sensitivity(clause),source_type,fields["domain"],fields["predicate"],fields["subject_text"],fields["object_text"],json.dumps(fields["qualifiers"],ensure_ascii=False),fields["valid_from"],fields["valid_to"],fields["observed_at"] or utc_now(),fields["durability"],json.dumps(fields["entities"],ensure_ascii=False),validation["security_state"],json.dumps(validation["security_findings"],ensure_ascii=False),clause_index,EXTRACTION_VERSION,event_profile or card_profile,unit_workspace,unit_visibility,event_agent or origin_agent_id or card_agent or "",(event_agent or owner_agent_id) if unit_visibility=="agent" else None))
                if cursor.rowcount: created.append({"unit_id":int(cursor.lastrowid),"raw_event_id":event_id,"clause_index":clause_index,"predicate":fields["predicate"]})
        if not dry_run and max_seen > int(last_extracted or 0):
            remaining = conn.execute("SELECT EXISTS(SELECT 1 FROM raw_events WHERE session_card_id=? AND id>?)", (cid, max_seen)).fetchone()[0]
            conn.execute("UPDATE session_cards SET last_extracted_event_id=?, needs_extraction=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",(max_seen,1 if remaining else 0,cid))
    if not dry_run: conn.commit()
    conn.close(); return {"status":"ok","dry_run":dry_run,"created":created,"skipped":skipped,"card_count":len(cards)}


def main() -> None:
    args=parse_args(); emit(extract_units(store_root(args.store),subject_id=args.subject_id,session_id=args.session_id,card_id=args.card_id,event_start_id=args.event_start_id,event_end_id=args.event_end_id,limit=args.limit,include_assistant=args.include_assistant,dry_run=args.dry_run,profile_id=args.profile_id,workspace_id=args.workspace_id,origin_agent_id=args.agent_id,visibility_scope=args.visibility_scope,owner_agent_id=args.agent_id))


if __name__ == "__main__": main()
