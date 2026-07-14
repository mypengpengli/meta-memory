"""One policy gate for automatically extracted memory plans.

The extraction pipeline can propose a semantic operation, but only this module
decides whether a background worker may make it readable without a human
review.  Explicit ``remember`` and ``correct`` actions remain synchronous.
"""
from __future__ import annotations

from typing import Mapping


VALID_MEMORY_MODES = {"manual", "conservative", "automatic"}
_USER_SOURCES = {"conversation-user", "explicit-memory", "user-memory-feedback"}
_AUTO_ACTIONS = {"CREATE", "CORROBORATE", "REFINE"}
_STATE_CHANGE_WORDS = ("现在", "已经改成", "从今天开始", "迁移到", "不再使用", "升级为", "now", "migrated", "switched")


def normalize_mode(value: str | None) -> str:
    mode = str(value or "").strip().casefold()
    return mode if mode in VALID_MEMORY_MODES else "automatic"


def decide_action(
    action: Mapping[str, object],
    *,
    memory_mode: str,
    explicit_user_action: bool = False,
) -> str:
    """Return ``auto_apply``, ``stage``, or ``ignore`` for one plan action."""
    if explicit_user_action or bool(action.get("explicit_user_action")):
        return "auto_apply"
    name = str(action.get("action") or "").upper()
    if name in {"", "IGNORE", "NOT_MEMORY"}:
        return "ignore"
    mode = normalize_mode(memory_mode)
    if mode == "manual":
        return "stage"

    source_type = str(action.get("source_type") or "").casefold()
    verification = str(action.get("verification_state") or "").casefold()
    sensitivity = str(action.get("sensitivity") or "normal").casefold()
    confidence = float(action.get("confidence") or 0.0)
    prompt_eligible = bool(action.get("prompt_eligible", True))
    requires_review = bool(action.get("requires_review"))
    # A document can create a reviewable, non-prompt candidate in Automatic
    # mode.  It can never become a factual Claim merely because the document
    # says so; manual and conservative modes still leave it staged.
    if source_type == "resource":
        return (
            "auto_apply"
            if mode == "automatic"
            and name == "CREATE"
            and str(action.get("memory_kind") or "").casefold() == "candidate"
            and verification == "resource"
            and not prompt_eligible
            else "stage"
        )
    user_backed = source_type in _USER_SOURCES or not source_type
    # ``requires_review`` originates in the conservative consolidation plan.
    # It is not, by itself, evidence that an otherwise well-sourced action is
    # unsafe: the automatic policy is the single place that may relax that
    # default.  Keep an explicit conflict marker separate so that a detected
    # contradiction never gets promoted merely because it came from the user.
    evidence_safe = (
        user_backed
        and verification == "verified"
        and sensitivity == "normal"
        and confidence >= 0.80
        and prompt_eligible
        and not bool(action.get("risk_reason"))
    )
    low_risk = evidence_safe and not requires_review
    if mode == "conservative":
        return "auto_apply" if name in {"CREATE", "CORROBORATE"} and low_risk else "stage"

    # Automatic still never treats a model/assistant response as a user fact,
    # and a correction requires the user's explicit correct command.
    if name == "REFINE":
        return "auto_apply" if evidence_safe and bool(action.get("refine_safe")) else "stage"
    if name in _AUTO_ACTIONS and evidence_safe:
        return "auto_apply"
    if name == "SUPERSEDE" and evidence_safe:
        content = str(action.get("content") or "").casefold()
        relation = str(action.get("relation") or "").upper()
        if relation == "REPLACES_OLD_STATE" and any(token in content for token in _STATE_CHANGE_WORDS):
            return "auto_apply"
    return "stage"
