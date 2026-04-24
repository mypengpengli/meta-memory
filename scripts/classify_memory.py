#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

from _common import emit


LONG_TERM_KINDS = ["profile", "state", "event", "relationship", "goal", "domain"]
ALL_KINDS = LONG_TERM_KINDS + ["session", "candidate"]

KIND_RULES: dict[str, list[tuple[str, float, str]]] = {
    "profile": [
        (r"(长期|一直|一贯|向来|核心习惯|长期偏好|稳定偏好|稳定习惯|价值观|原则|风格|long term|long-term|core habit|value|principle|style)", 2.4, "long-term/stable wording"),
        (r"(偏好|喜欢|不喜欢|习惯|通常|总是|倾向于|prefer|preference|habit|usually|always|tends to)", 1.7, "preference/habit wording"),
    ],
    "state": [
        (r"(最近|近期|目前|现在|这段时间|这两周|这几个月|近来|当下|recent|recently|currently|lately|last two months|these weeks|these months|current)", 2.2, "current-period wording"),
        (r"(状态|压力|情绪|睡眠|作息|疲劳|精力|身体状态|工作状态|家庭状态|state|pressure|stress|mood|sleep|fatigue|energy|working state|family state)", 1.9, "state wording"),
    ],
    "event": [
        (r"((19|20)\d{2}年|\d{4}-\d{2}-\d{2}|那次|发生|开始|结束|之后|当时|经历|转折|节点|失败|毕业|搬家|生病|happened|started|ended|after|at that time|experience|turning point|failure|graduated|moved|got sick)", 2.3, "event/timeline wording"),
        (r"(后来|从那以后|随后|当年|曾经|later|after that|back then|once)", 1.6, "historical wording"),
    ],
    "relationship": [
        (r"(孩子|父母|伴侣|妻子|丈夫|朋友|同事|客户|家人|老师|同学|child|children|parent|partner|wife|husband|friend|colleague|client|family|teacher|classmate)", 2.0, "people wording"),
        (r"(沟通|相处|边界|敏感|冲突|信任|协作|关系|安抚|支持|communicat|boundary|sensitive|conflict|trust|cooperate|relationship|comfort|support)", 2.0, "relationship wording"),
    ],
    "goal": [
        (r"(目标|计划|项目|重点目标|年度重点|今年重点|今年的重点|今年|年度|路线|里程碑|打算|想做成|推进|阶段目标|下一阶段|goal|plan|project|focus|annual focus|this year|roadmap|milestone|priority|want to build|move forward|next phase)", 2.8, "goal/project wording"),
        (r"(约束|阻塞|待办|下一步|路线|方向|constraint|blocker|todo|next step|roadmap|direction)", 1.4, "project management wording"),
    ],
    "domain": [
        (r"(经验|方法|坑点|复盘|做法|判断|流程|规则|方法论|案例|教训|可复用|experience|method|pitfall|retrospective|approach|judgment|process|rule|case|lesson|reusable)", 2.0, "domain knowledge wording"),
        (r"(编程|部署|调试|测试|学习|训练|消费|预算|运动|饮食|通勤|居住|配置|接口|排查|调用|coding|deployment|debug|testing|learning|training|spending|budget|exercise|diet|commute|housing|config|api|investigation|call)", 1.3, "domain slice wording"),
    ],
    "session": [
        (r"(这轮|当前|正在|现在在|做到哪|下一步|待确认|排查中|还没完全验证|继续查|临时结论|this round|currently|in progress|where we are|next step|needs confirmation|investigating|not fully verified|temporary conclusion)", 2.6, "session/in-progress wording"),
        (r"(todo|wip|next step|investigating|in progress|not verified yet)", 2.2, "session English wording"),
    ],
    "candidate": [
        (r"(可能|也许|似乎|怀疑|待观察|继续观察|不确定|未验证|待验证|需要确认|像是|暂时|先记下来|maybe|might|may be|needs observation|still needs observation|uncertain|unverified|needs verification|needs confirmation|for now|note this down)", 3.1, "uncertainty wording"),
        (r"(和旧记忆冲突|修正旧说法|补充旧记忆|可能要更新|conflict with old memory|update old memory|patch old memory)", 2.1, "conflict/update wording"),
    ],
}

DOMAIN_RULES: dict[str, list[tuple[str, float, str]]] = {
    "work": [
        (r"(编程|代码|部署|调试|测试|架构|接口|模型|prompt|agent|项目|产品|业务|工具|OpenCowork|thinking|code|deployment|debug|test|architecture|api|model|project|product|business|tool)", 1.8, "work keyword"),
    ],
    "learning": [
        (r"(学习|训练|阅读|课程|知识|笔记|复盘|练习|记忆|理解|learning|training|reading|course|knowledge|note|practice|memory|understanding)", 1.8, "learning keyword"),
    ],
    "health": [
        (r"(健康|睡眠|作息|饮食|运动|疲劳|精力|医疗|恢复|身体|health|sleep|routine|diet|exercise|fatigue|energy|medical|recovery|body)", 1.8, "health keyword"),
    ],
    "finance": [
        (r"(财务|预算|消费|购买|支出|现金流|资产|订阅|风险偏好|投资|价格|finance|budget|spending|purchase|expense|cash flow|asset|subscription|risk preference|invest|price)", 1.8, "finance keyword"),
    ],
    "relationships": [
        (r"(关系|沟通|边界|冲突|修复|孩子|父母|伴侣|朋友|同事|客户|家人|情绪支持|安抚|relationship|communication|boundary|conflict|repair|child|parent|partner|friend|colleague|client|family|emotional support|comfort)", 2.2, "relationship domain keyword"),
    ],
    "daily-life": [
        (r"(生活|家务|居住|通勤|出行|事务|收纳|设备|居家|日常安排|life|housework|housing|commute|travel|errand|storage|device|home|daily schedule)", 1.8, "daily-life keyword"),
    ],
}

TAG_RULES: list[tuple[str, str]] = [
    (r"(偏好|喜欢|不喜欢)", "偏好"),
    (r"(习惯|通常|总是)", "习惯"),
    (r"(压力|焦虑|疲劳|情绪)", "状态"),
    (r"(睡眠|作息)", "作息"),
    (r"(孩子|父母|伴侣|朋友|同事|客户)", "人物"),
    (r"(沟通|边界|冲突|协作)", "沟通"),
    (r"(目标|计划|项目|路线|里程碑)", "目标"),
    (r"(方法|经验|坑点|复盘|教训)", "经验"),
    (r"(未验证|待确认|不确定|待观察)", "未验证"),
    (r"(时间|节点|事件|之后|那次)", "事件"),
]

HISTORICAL_RULES = [
    r"((19|20)\d{2}年|当时|后来|曾经|那次|之前|之后|从那以后)",
]

ACTIVE_RULES = [
    r"(最近|目前|现在|当前|这段时间|持续|长期|一直)",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Classify a new memory into the right memory layer.")
    parser.add_argument("--title", help="Memory title")
    parser.add_argument("--content", help="Inline memory content")
    parser.add_argument("--content-file", help="Read content from a UTF-8 text file")
    parser.add_argument("--payload-file", help="Read title/content from a UTF-8 JSON file")
    parser.add_argument("--subject-id", default="person-unknown", help="Primary subject id")
    parser.add_argument("--subject-name", default="Unknown", help="Primary subject display name")
    parser.add_argument("--out-file", help="Write classification JSON to a UTF-8 file")
    return parser.parse_args()


def load_payload(path: str | None) -> dict[str, object]:
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def read_content(args: argparse.Namespace, payload: dict[str, object]) -> tuple[str, str]:
    title = args.title or str(payload.get("title", "")).strip()
    if args.content_file:
        content = Path(args.content_file).read_text(encoding="utf-8-sig").strip()
    elif args.content:
        content = args.content.strip()
    else:
        content = str(payload.get("content", "")).strip()
    if not title:
        title = first_sentence(content)[:40] or "Untitled Memory"
    if not content:
        raise SystemExit("Content is required via --content, --content-file, or --payload-file.")
    return title, content


def first_sentence(text: str) -> str:
    match = re.split(r"[。！？!?.\n]+", text.strip(), maxsplit=1)
    return match[0].strip() if match else ""


def slugify(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." or ("\u4e00" <= ch <= "\u9fff") else "-" for ch in text.strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-_.") or "memory"


def lower_text(title: str, content: str) -> str:
    return f"{title}\n{content}".casefold()


def score_rules(text: str, rules: list[tuple[str, float, str]]) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    for pattern, weight, reason in rules:
        if re.search(pattern, text, flags=re.IGNORECASE):
            score += weight
            reasons.append(reason)
    return score, reasons


def score_kinds(text: str) -> tuple[dict[str, float], dict[str, list[str]]]:
    scores: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}
    for kind in ALL_KINDS:
        score, why = score_rules(text, KIND_RULES[kind])
        scores[kind] = score
        reasons[kind] = why

    if scores["candidate"] >= 2.0:
        for kind in LONG_TERM_KINDS:
            scores[kind] -= 0.6
    if scores["candidate"] >= 3.0:
        for kind in ["profile", "state", "event", "relationship", "goal", "domain"]:
            scores[kind] -= 0.8
        if re.search(r"(继续观察|待观察|未验证|待确认|不确定|可能|still needs observation|needs observation|unverified|needs confirmation|maybe|might)", text, flags=re.IGNORECASE):
            scores["candidate"] += 0.8
    if scores["session"] >= 2.0:
        for kind in ["profile", "relationship", "domain"]:
            scores[kind] -= 0.4
    if scores["event"] >= 2.0 and scores["state"] >= 1.5:
        scores["event"] += 0.4
    if scores["relationship"] >= 2.0:
        scores["state"] += 0.2
    return scores, reasons


def score_domains(text: str) -> tuple[str, dict[str, float], list[str]]:
    domain_scores: dict[str, float] = defaultdict(float)
    reasons: list[str] = []
    for domain, rules in DOMAIN_RULES.items():
        score, why = score_rules(text, rules)
        domain_scores[domain] += score
        if why:
            reasons.extend(f"{domain}:{item}" for item in why)
    if not domain_scores:
        domain_scores["general"] = 0.0
    best = max(domain_scores.items(), key=lambda item: item[1])[0]
    if domain_scores[best] <= 0:
        best = "general"
    return best, dict(domain_scores), reasons[:5]


def suggest_tags(text: str, domain: str, kind: str) -> list[str]:
    tags: list[str] = []
    for pattern, tag in TAG_RULES:
        if re.search(pattern, text, flags=re.IGNORECASE) and tag not in tags:
            tags.append(tag)
    if domain != "general" and domain not in tags:
        tags.append(domain)
    if kind == "profile" and "长期" not in tags:
        tags.append("长期")
    if kind == "state" and "阶段" not in tags:
        tags.append("阶段")
    if kind == "event" and "事件" not in tags:
        tags.append("事件")
    if kind == "session" and "进行中" not in tags:
        tags.append("进行中")
    if kind == "candidate" and "未验证" not in tags:
        tags.append("未验证")
    return tags[:8]


def pick_kind(scores: dict[str, float]) -> tuple[str, str]:
    sorted_kinds = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    recommended = sorted_kinds[0][0]
    underlying = max(((kind, scores[kind]) for kind in LONG_TERM_KINDS), key=lambda item: item[1])[0]
    return recommended, underlying


def suggest_status(kind: str, text: str) -> str:
    if kind == "candidate":
        return "pending"
    if kind == "session":
        return "active"
    if kind == "event":
        return "historical"
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in HISTORICAL_RULES):
        return "historical"
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in ACTIVE_RULES):
        return "active"
    return "active"


def estimate_confidence(recommended: str, scores: dict[str, float]) -> float:
    ordered = sorted(scores.values(), reverse=True)
    top = ordered[0] if ordered else 0.0
    second = ordered[1] if len(ordered) > 1 else 0.0
    base = 0.45 + min(top / 8.0, 0.35) + min((top - second) / 6.0, 0.15)
    if recommended in {"candidate", "session"}:
        base -= 0.05
    return round(max(0.2, min(base, 0.95)), 2)


def recommend_action(kind: str) -> str:
    if kind == "session":
        return "write_to_session"
    if kind == "candidate":
        return "write_to_candidate"
    return "write_to_long_term"


def suggested_memory_confidence(kind: str, classification_confidence: float) -> float:
    if kind == "candidate":
        return min(classification_confidence, 0.35)
    if kind == "session":
        return min(classification_confidence, 0.5)
    return classification_confidence


def estimate_importance(kind: str, text: str, confidence: float) -> float:
    base = {
        "profile": 0.85,
        "state": 0.65,
        "event": 0.7,
        "relationship": 0.7,
        "goal": 0.75,
        "domain": 0.65,
        "session": 0.35,
        "candidate": 0.25,
    }.get(kind, 0.5)
    if re.search(r"(重要|关键|核心|必须|不能忘|长期|稳定|原则|偏好|目标|important|critical|core|must remember|long-term|principle|preference|goal)", text, flags=re.IGNORECASE):
        base += 0.12
    if re.search(r"(暂时|可能|也许|未验证|待确认|随口|maybe|temporary|unverified|uncertain)", text, flags=re.IGNORECASE):
        base -= 0.12
    base += max(0.0, confidence - 0.6) * 0.2
    return round(max(0.1, min(base, 1.0)), 2)


def build_reasons(
    recommended: str,
    underlying: str,
    kind_reasons: dict[str, list[str]],
    domain_reasons: list[str],
) -> list[str]:
    reasons: list[str] = []
    reasons.extend(kind_reasons.get(recommended, []))
    if recommended in {"session", "candidate"} and underlying != recommended:
        reasons.append(f"underlying long-term kind looks like {underlying}")
    reasons.extend(domain_reasons)
    unique: list[str] = []
    for item in reasons:
        if item and item not in unique:
            unique.append(item)
    return unique[:6]


def classify(title: str, content: str, subject_id: str, subject_name: str) -> dict[str, object]:
    text = lower_text(title, content)
    kind_scores, kind_reasons = score_kinds(text)
    recommended, underlying = pick_kind(kind_scores)
    domain, domain_scores, domain_reasons = score_domains(text)
    if recommended == "relationship":
        domain = "relationships"
    status = suggest_status(recommended, text)
    confidence = estimate_confidence(recommended, kind_scores)
    payload_confidence = suggested_memory_confidence(recommended, confidence)
    importance = estimate_importance(recommended, text, confidence)
    tags = suggest_tags(text, domain, recommended)
    reasons = build_reasons(recommended, underlying, kind_reasons, domain_reasons)

    result = {
        "status": "ok",
        "title": title,
        "recommended_kind": recommended,
        "underlying_long_term_kind": underlying,
        "recommended_domain": domain,
        "recommended_status": status,
        "classification_confidence": confidence,
        "recommended_action": recommend_action(recommended),
        "suggested_tags": tags,
        "reasons": reasons,
        "kind_scores": {key: round(value, 2) for key, value in sorted(kind_scores.items())},
        "domain_scores": {key: round(value, 2) for key, value in sorted(domain_scores.items())},
        "suggested_payload": {
            "title": title,
            "kind": recommended,
            "subject_id": subject_id,
            "subject_name": subject_name,
            "domain": domain,
            "topic": slugify(title),
            "content": content,
            "tags": tags,
            "status": status,
            "confidence": payload_confidence,
            "importance": importance,
        },
    }
    return result


def main() -> None:
    args = parse_args()
    payload = load_payload(args.payload_file)
    title, content = read_content(args, payload)
    result = classify(title, content, args.subject_id, args.subject_name)

    if args.out_file:
        Path(args.out_file).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(result)


if __name__ == "__main__":
    main()
