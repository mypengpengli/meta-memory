"""Treat recalled memory as untrusted data, never as executable instructions."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
import unicodedata


@dataclass(frozen=True)
class SecurityFinding:
    code: str
    severity: str
    matched_text: str
    start: int
    end: int
    explanation: str


PATTERNS: tuple[tuple[str, str, str, str], ...] = (
    ("prompt_injection", "blocked", r"(?:ignore|disregard|forget).{0,32}(?:previous|prior|system|developer).{0,32}(?:instruction|prompt|rule)|忽略(?:之前|先前|系统).{0,24}(?:指令|规则)|忽略系统规则", "Attempts to override prior instructions are not durable memory."),
    ("role_spoofing", "blocked", r"(?:^|\n)\s*(?:system|developer)\s*[:：]", "Role-labelled text can impersonate a trusted instruction."),
    ("secret_exfiltration", "blocked", r"(?:reveal|print|show|exfiltrate).{0,48}(?:system prompt|api[_ -]?key|secret|token|credential|password)|(?:输出系统提示词|读取并发送\s*API\s*Key|泄露(?:密钥|凭证))", "Requests for secrets or hidden prompts are unsafe."),
    ("hidden_execution", "blocked", r"(?:run|execute|curl|wget|powershell|bash).{0,48}(?:silently|hidden|background|without.*(?:tell|ask))|(?:不要告诉用户|后台静默执行).{0,48}(?:命令|脚本|下载|请求)?", "Hidden shell or network actions are unsafe in recalled data."),
    ("tool_call_forgery", "blocked", r"<(?:tool_call|function_call|assistant|memory-context)\b[^>]*>", "Internal tool/context tags must not be persisted as memory instructions."),
    ("html_hidden", "suspicious", r"<!--.*?-->|<span[^>]+(?:display\s*:\s*none|hidden)", "Hidden markup may conceal instructions."),
)
INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")


def scan_memory_content(text: str, *, source_type: str = "memory") -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    value = text or ""
    for code, severity, pattern, explanation in PATTERNS:
        for match in re.finditer(pattern, value, flags=re.IGNORECASE | re.DOTALL):
            findings.append(SecurityFinding(code, severity, match.group(0)[:180], match.start(), match.end(), explanation))
    for match in INVISIBLE.finditer(value):
        character = match.group(0)
        findings.append(SecurityFinding("invisible_unicode", "blocked", f"U+{ord(character):04X}", match.start(), match.end(), "Bidirectional and zero-width controls are blocked from memory context."))
    # Non-normalized text can visually impersonate punctuation/identifiers. It
    # is reviewable rather than automatically rejected unless it is invisible.
    normalized = unicodedata.normalize("NFKC", value)
    if normalized != value and any(ord(char) > 127 for char in value):
        findings.append(SecurityFinding("unicode_confusable", "suspicious", "unicode-normalization-diff", 0, len(value), "Unicode normalization changes the recalled text."))
    return findings


def findings_json(findings: list[SecurityFinding]) -> list[dict[str, object]]:
    return [asdict(item) for item in findings]


def security_state(findings: list[SecurityFinding]) -> tuple[str, int]:
    if any(item.severity == "blocked" for item in findings):
        return "blocked", 0
    if findings:
        return "suspicious", 0
    return "clean", 1


def sanitize_for_context(text: str) -> str:
    """Remove control characters and quote data that will be injected as context."""
    return INVISIBLE.sub("", text or "").replace("<memory-context", "&lt;memory-context").replace("</memory-context", "&lt;/memory-context")
