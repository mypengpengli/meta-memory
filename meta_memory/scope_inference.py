"""Conservative promotion of user-wide preferences to global memory scope.

The generic unit classifier is intentionally broad and can call a sentence a
``domain`` or ``procedure`` even when the user is plainly specifying how every
Agent should answer.  Scope must be decided separately from memory kind: a
technical project instruction must stay local, while a stable response
preference should follow the user between projects.
"""
from __future__ import annotations

import re


_USER_SOURCES = {"conversation-user", "explicit-memory", "user-memory-feedback"}
_RESPONSE_WORDS = re.compile(
    r"(?:answer|respond|response|reply|language|format|tone|concise|verbose|"
    r"回答|回复|答复|语言|输出|说明|简洁|详细|语气)",
    re.IGNORECASE,
)
_GLOBAL_WORDS = re.compile(
    r"(?:always|every\s+time|from\s+now\s+on|all\s+agents?|across\s+projects?|"
    r"以后|今后|始终|一直|每次|所有(?:的)?(?:agent|agents|助手|智能体)|跨项目)",
    re.IGNORECASE,
)
_LANGUAGE_WORDS = re.compile(r"(?:chinese|english|中文|英文|汉语|英语)", re.IGNORECASE)
_PROJECT_MARKERS = re.compile(
    r"(?:repo(?:sitory)?|project|workspace|database|migration|deploy|code|"
    r"项目|仓库|工作区|数据库|迁移|部署|代码|分支)",
    re.IGNORECASE,
)
_CROSS_PROJECT = re.compile(r"(?:across\s+all\s+projects?|all\s+projects?|跨项目|所有项目)", re.IGNORECASE)


def is_global_user_memory(content: str, *, unit_kind: str = "", source_type: str = "") -> bool:
    """Return true only for stable, user-authored cross-project preferences."""
    if str(source_type or "").casefold() not in _USER_SOURCES:
        return False
    text = " ".join(str(content or "").split())
    if not text:
        return False
    # "For this project ..." is deliberately local even when it says
    # "always".  A project marker may be overridden only by an equally
    # explicit all-project signal.
    if _PROJECT_MARKERS.search(text) and not _CROSS_PROJECT.search(text):
        return False
    response_preference = bool(_RESPONSE_WORDS.search(text))
    global_signal = bool(_GLOBAL_WORDS.search(text))
    language_preference = bool(_LANGUAGE_WORDS.search(text)) and (response_preference or global_signal)
    # A normal profile unit is user-wide unless it is explicitly describing a
    # project artifact.  This preserves the existing profile behavior while
    # avoiding accidental promotion of technical project state.
    if str(unit_kind or "").casefold() == "profile" and not _PROJECT_MARKERS.search(text):
        return True
    return bool(global_signal and (response_preference or language_preference))


def inferred_visibility(content: str, *, unit_kind: str = "", source_type: str = "") -> str:
    return "global" if is_global_user_memory(content, unit_kind=unit_kind, source_type=source_type) else "workspace"
