"""Canonical escape token and gate policy for hook decisions."""

import hashlib
import json
import re
from enum import StrEnum

_TOKEN_RE = re.compile(r"\[gate-escape:([a-z0-9_.-]+):\s*(\S[^\]]*?)\s*\]")
_STRIP_RE = re.compile(r"[ \t]*(?:#[ \t]*)?\[gate-escape:[a-z0-9_.-]+:[^\]]*\]")
_MAX_NESTING = 3
_SCAN_WINDOW = 512
_EXCERPT_LIMIT = 200


class EscapePolicy(StrEnum):
    AUDITED = "audited"
    HUMAN_ONLY = "human_only"
    NEVER = "never"


_NEVER = frozenset(
    {
        "ai_signature",
        "banned_terms",
        "bot_push",
        "dispatch_quote_scanner",
        "foreign_branch_push_force_delete",
        "quote_scanner",
        "secret_file_print",
        "self_dm",
        "verbatim_operator_paste",
        "void_merge",
    }
)
_HUMAN_ONLY = frozenset({"out_of_band_merge", "raw_review_post", "raw_issue_write"})
_AUDITED = frozenset(
    {
        "block-standing-grant-ask",
        "block-cron-loop-shell",
        "brief_anchor",
        "classifier_relax",
        "config_overwrite",
        "deferred_question",
        "diff_coverage",
        "direct_command",
        "dispatch_admission",
        "foreign_branch_push",
        "general_purpose_agent_gate",
        "git_add_all",
        "glab_stale_base_remote",
        "headless_authoring",
        "main_clone",
        "mcp_slack_write",
        "mr_metadata",
        "no_self_reviewer_assign",
        "orchestrator_bash",
        "orchestrator_delegation_gate",
        "orchestrator_foreground_dispatch",
        "owner-question-decision",
        "owner-question-fit",
        "plan_gate",
        "protect_default_branch",
        "raw_pid_kill",
        "single_branch_repo",
        "skill-loading-enforcement",
        "unapprovable_author_create",
        "unbounded_wait",
        "unknown_repo_push",
        "visible_plan_gate",
        "worktree_containment",
        "answer_first",
        "completion_claim",
        "unbacked_claim",
        "standing_goal_stop",
    }
)
KNOWN_GATES = _NEVER | _HUMAN_ONLY | _AUDITED


def policy_for(gate_id: str) -> EscapePolicy:
    if gate_id in _AUDITED:
        return EscapePolicy.AUDITED
    if gate_id in _HUMAN_ONLY:
        return EscapePolicy.HUMAN_ONLY
    return EscapePolicy.NEVER


def _text_values(value: object, depth: int = 0) -> list[str]:
    if depth > _MAX_NESTING:
        return []
    if isinstance(value, str):
        return [value] if len(value) <= 2 * _SCAN_WINDOW else [value[:_SCAN_WINDOW], value[-_SCAN_WINDOW:]]
    if isinstance(value, dict):
        return [text for key, child in value.items() if key != "description" for text in _text_values(child, depth + 1)]
    if isinstance(value, list):
        return [text for child in value for text in _text_values(child, depth + 1)]
    return []


def find_escape(value: object, gate_id: str) -> str | None:
    for text in _text_values(value):
        for match in _TOKEN_RE.finditer(text):
            reason = match.group(2).strip()
            if match.group(1) == gate_id and not (reason.startswith("<") and reason.endswith(">")):
                return reason
    return None


def strip_escape(text: str) -> str:
    return _STRIP_RE.sub("", text).strip()


def _without_escape(value: object) -> object:
    if isinstance(value, str):
        return strip_escape(value)
    if isinstance(value, dict):
        return {key: _without_escape(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_without_escape(child) for child in value]
    return value


def action_digest(tool_name: str, tool_input: object) -> str:
    payload = json.dumps(
        {"tool_name": tool_name, "tool_input": _without_escape(tool_input)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def action_excerpt(value: object) -> str:
    text = value.get("command") if isinstance(value, dict) else value
    if not isinstance(text, str):
        text = json.dumps(_without_escape(value), sort_keys=True, ensure_ascii=False, default=str)
    return " ".join(strip_escape(text).split())[:_EXCERPT_LIMIT]
